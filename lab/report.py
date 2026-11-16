"""Summarise results/runs.jsonl as markdown tables.

  uv run python -m lab.report --tag pilot
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from .tickets import RULES

RUNS = Path(__file__).resolve().parent.parent / "results" / "runs.jsonl"
CHANNELS = ["system", "kickoff", "tool", "midrun"]


def frac(v: int, p: int) -> str:
    return f"{v}/{p}" if p else "-"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="pilot")
    args = ap.parse_args()
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for line in RUNS.read_text().splitlines():
        r = json.loads(line)
        if r["tag"] == args.tag:
            cells[(r["model"], r["strategy"], r["tickets"])].append(r)
    if not cells:
        print(f"no runs tagged {args.tag}")
        return

    print(f"## Runs tagged `{args.tag}`\n")
    print("| model | strategy | runs | tickets done | tasks ok | rule violations | " + " | ".join(CHANNELS) +
          " | by quarter | repeats | compactions | clears | peak context | calls | cost/run | min/run |")
    print("|---|---|---|---|---|---|" + "---|" * (len(CHANNELS) + 8))
    for (model, strategy, n), rs in sorted(cells.items()):
        k = len(rs)
        S = [r["score"] for r in rs]
        A = [r["agent"] for r in rs]
        ch = [frac(sum(s["channels"][c]["violations"] for s in S), sum(s["channels"][c]["probes"] for s in S)) for c in CHANNELS]
        quarters = " ".join(frac(sum(s["quarters"][q]["violations"] for s in S), sum(s["quarters"][q]["probes"] for s in S)) for q in range(4))
        print(f"| {model} | {strategy} | {k} | {sum(a['tickets_done'] for a in A)}/{n * k} | "
              f"{sum(s['tasks_completed'] for s in S)}/{sum(s['tickets_scored'] for s in S)} | "
              f"{frac(sum(s['rule_violations'] for s in S), sum(s['rule_probes'] for s in S))} | " + " | ".join(ch) +
              f" | {quarters} | {sum(s['repeated_migrations'] for s in S)} | {sum(a['compactions'] for a in A) / k:.1f} | "
              f"{sum(a['clear_events'] for a in A) / k:.1f} | {max(a['peak_context'] for a in A):,} | {sum(a['model_calls'] for a in A) / k:.0f} | "
              f"${sum(a['cost_usd'] for a in A) / k:.2f} | {sum(r['elapsed_s'] for r in rs) / k / 60:.1f} |")

    print("\n## Violations per rule (violations/probes, turns where it broke)\n")
    print("| model | strategy | " + " | ".join(RULES) + " |")
    print("|---|---|" + "---|" * len(RULES))
    for (model, strategy, _), rs in sorted(cells.items()):
        row = []
        for rule in RULES:
            v = sum(r["score"]["rules"][rule]["violations"] for r in rs)
            p = sum(r["score"]["rules"][rule]["probes"] for r in rs)
            at = sorted(t for r in rs for t in r["score"]["rules"][rule]["violated_at"])
            row.append(frac(v, p) + (f" @{','.join(map(str, at[:8]))}{'…' if len(at) > 8 else ''}" if at else ""))
        print(f"| {model} | {strategy} | " + " | ".join(row) + " |")
    stopped = [(r["run_id"], r["agent"]["stopped_by"], r["agent"].get("error")) for rs in cells.values() for r in rs if r["agent"]["stopped_by"] != "done"]
    if stopped:
        print("\nRuns that did not finish:", *stopped, sep="\n- ")


if __name__ == "__main__":
    main()
