"""Run shifts and append one record per run to results/runs.jsonl.

  uv run python -m lab.run --model claude-haiku-5-5 --strategies full --tickets 60 --tag smoke
  uv run python -m lab.run --model claude-sonnet-5-5 --strategies full,compact,clear,memory,memory+clear --repeat 3 --tag main --resume

Runs are strictly sequential (other labs share the same API keys).
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv

from .agent import STRATEGIES, TRIGGER, dump, run_shift
from .scorer import score
from .tickets import build_shift

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"


def one_run(model: str, strategy: str, n: int, seed: int, tag: str, effort: str, max_usd: float, limit: int | None) -> dict:
    shift = build_shift(n, seed)
    world = shift.make_world(seed)
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{strategy.replace('+', '_')}-{model.split('-')[1]}-{uuid.uuid4().hex[:4]}"
    print(f"\n=== {run_id}: {model} | {strategy} | {n} tickets | seed {seed}", flush=True)
    t0 = time.time()
    res = run_shift(shift, world, model, strategy, effort=effort, max_usd=max_usd, limit=limit, progress=lambda s: print(s, flush=True))
    elapsed = round(time.time() - t0, 1)
    sc = score(shift, world.log, upto=res.tickets_done or 1)
    raw = RESULTS / "raw" / run_id
    raw.mkdir(parents=True)
    (raw / "actions.json").write_text(dump(world.log))
    (raw / "calls.json").write_text(dump(res.calls))
    (raw / "summaries.json").write_text(dump(res.summaries))
    (raw / "memory.json").write_text(dump({"files": res.memory_files, "ops": res.memory_ops}))
    (raw / "per_ticket.json").write_text(dump(sc.pop("per_ticket")))
    agent = {k: v for k, v in res.__dict__.items() if k not in ("calls", "summaries", "memory_files", "memory_ops")}
    agent["cost_usd"] = round(res.cost_usd, 4)
    agent["memory_ops"] = len(res.memory_ops)
    agent["memory_bytes"] = sum(map(len, res.memory_files.values()))
    record = {"run_id": run_id, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tag": tag, "model": model,
              "strategy": strategy, "tickets": n, "seed": seed, "effort": effort, "trigger": TRIGGER,
              "sdk_version": version("anthropic"), "elapsed_s": elapsed, "agent": agent, "score": sc}
    with (RESULTS / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    print(f"    {res.stopped_by}: {res.tickets_done}/{n} tickets, tasks ok {sc['tasks_completed']}, violations {sc['rule_violations']}/{sc['rule_probes']} "
          f"{ {c: v['violations'] for c, v in sc['channels'].items()} } calls={res.model_calls} peak_ctx={res.peak_context:,} "
          f"compactions={res.compactions} clears={res.clear_events} cost=${res.cost_usd:.3f} {elapsed}s" + (f" ERROR {res.error}" if res.error else ""), flush=True)
    return record


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--strategies", default="full", help=f"comma-separated: {','.join(STRATEGIES)}")
    ap.add_argument("--tickets", type=int, default=120)
    ap.add_argument("--limit", type=int, help="stop after this many tickets of the shift (plumbing checks)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--max-usd", type=float, default=5.0, help="per-run spend cap; the run stops after the ticket that crosses it")
    ap.add_argument("--max-total-usd", type=float, default=10.0, help="stop the batch once this much is spent")
    ap.add_argument("--tag", default="pilot")
    ap.add_argument("--resume", action="store_true", help="skip runs this tag already has (errored runs don't count)")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    done: dict[tuple, int] = {}
    if args.resume and (RESULTS / "runs.jsonl").exists():
        for line in (RESULTS / "runs.jsonl").read_text().splitlines():
            r = json.loads(line)
            if r["tag"] == args.tag and r["agent"]["stopped_by"] == "done":
                key = (r["model"], r["strategy"], r["tickets"], r["seed"])
                done[key] = done.get(key, 0) + 1
    spent = 0.0
    for trial in range(args.repeat):
        for strategy in args.strategies.split(","):
            if done.get((args.model, strategy, args.tickets, args.seed), 0) > trial:
                continue
            if spent >= args.max_total_usd:
                print(f"batch cap ${args.max_total_usd} reached, stopping", flush=True)
                return
            rec = one_run(args.model, strategy, args.tickets, args.seed, args.tag, args.effort, min(args.max_usd, args.max_total_usd - spent), args.limit)
            spent += rec["agent"]["cost_usd"]
    print(f"\nbatch spend: ${spent:.3f}", flush=True)


if __name__ == "__main__":
    main()
