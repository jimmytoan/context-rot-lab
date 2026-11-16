"""The model-driven agent: one long conversation on the Claude Messages API, one context strategy per run.

The loop is hand-written on purpose so that the only thing that differs between strategies is the
`context_management` config and whether the memory tool is offered. Feature names verified against
platform.claude.com docs on 2026-11-15 (see README).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import anthropic

from .tickets import KICKOFF, SYSTEM_PROMPT, Shift
from .world import TOOL_SPECS, World

TRIGGER = 50_000   # tokens. 50,000 is the lowest value threshold compaction accepts; clearing uses the same number.

COMPACT = {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": TRIGGER}}
CLEAR = {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": TRIGGER},
         "keep": {"type": "tool_uses", "value": 6}, "clear_at_least": {"type": "input_tokens", "value": 10_000}}
MEMORY_TOOL = {"type": "memory_20250818", "name": "memory"}

# name -> (context_management edits, beta headers, offer the memory tool)
STRATEGIES: dict[str, tuple[list[dict], list[str], bool]] = {
    "full": ([], [], False),
    "compact": ([COMPACT], ["compact-2026-01-12"], False),
    "clear": ([CLEAR], ["context-management-2025-06-27"], False),
    "memory": ([], [], True),
    "memory+clear": ([{**CLEAR, "exclude_tools": ["memory"]}], ["context-management-2025-06-27"], True),
}

# USD per token: (input, cache write 5m, cache read, output). Source: LiteLLM 1.109.1 price map, matches Anthropic list prices.
PRICES = {
    "claude-sonnet-5-5": {"base": (2e-6, 2.5e-6, 1e-7, 1e-5)},
    "claude-haiku-5-5": {"base": (1e-7, 1.25e-7, 1e-8, 5e-7), "above_100k": (5e-7, 6.25e-7, 5e-8, 2.5e-6)},
    "claude-opus-5-5": {"base": (4e-6, 5e-6, 2e-7, 2e-5)},
}


def call_cost(model: str, inp: int, cwrite: int, cread: int, out: int) -> float:
    p = PRICES[model]
    tier = p["above_100k"] if "above_100k" in p and inp + cwrite + cread > 100_000 else p["base"]
    return inp * tier[0] + cwrite * tier[1] + cread * tier[2] + out * tier[3]


class MemoryStore:
    """Client-side backend for the memory tool: the six documented commands over an in-memory /memories tree."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.ops: list[dict] = []

    def handle(self, args: dict, ticket_idx: int) -> str:
        cmd = args.get("command")
        self.ops.append({"ticket_idx": ticket_idx, "command": cmd, "path": args.get("path") or args.get("old_path")})
        path = args.get("path") or args.get("old_path") or ""
        if not path.startswith("/memories") or ".." in path:
            return f"Error: path must be inside /memories: {path}"
        f = self.files
        if cmd == "view":
            if path in f:
                return f"Here's the content of {path} with line numbers:\n" + "\n".join(f"{i + 1:>6}\t{ln}" for i, ln in enumerate(f[path].splitlines()))
            inside = sorted(p for p in f if p.startswith(path.rstrip("/") + "/"))
            if path.rstrip("/") != "/memories" and not inside:
                return f"The path {path} does not exist. Please provide a valid path."
            return "Here're the files and directories up to 2 levels deep in /memories, excluding hidden items and node_modules:\n" + \
                "\n".join(["4.0K\t/memories"] + [f"{max(1, len(f[p]) // 1024)}K\t{p}" for p in inside])
        if cmd == "create":
            f[path] = args.get("file_text", "")
            return f"File created successfully at: {path}"
        if cmd == "str_replace":
            if path not in f:
                return f"Error: The path {path} does not exist. Please provide a valid path."
            old = args.get("old_str", "")
            if f[path].count(old) != 1:
                return f"No replacement was performed, old_str `{old}` did not appear exactly once in {path}."
            f[path] = f[path].replace(old, args.get("new_str", ""))
            return "The memory file has been edited."
        if cmd == "insert":
            if path not in f:
                return f"Error: The path {path} does not exist"
            lines = f[path].splitlines()
            at = int(args.get("insert_line", len(lines)))
            if not 0 <= at <= len(lines):
                return f"Error: Invalid `insert_line` parameter: {at}. It should be within the range of lines of the file: [0, {len(lines)}]"
            lines[at:at] = args.get("insert_text", "").splitlines()
            f[path] = "\n".join(lines) + "\n"
            return f"The file {path} has been edited."
        if cmd == "delete":
            gone = [p for p in f if p == path or p.startswith(path.rstrip("/") + "/")]
            if not gone or path.rstrip("/") == "/memories":
                return f"Error: The path {path} does not exist"
            for p in gone:
                del f[p]
            return f"Successfully deleted {path}"
        if cmd == "rename":
            new = args.get("new_path", "")
            if path not in f or not new.startswith("/memories") or ".." in new or new in f:
                return f"Error: cannot rename {path} to {new}"
            f[new] = f.pop(path)
            return f"Successfully renamed {path} to {new}"
        return f"Error: unknown command {cmd}"


@dataclass
class RunResult:
    tickets_done: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    compactions: int = 0
    clear_events: int = 0
    cleared_tool_uses: int = 0
    peak_context: int = 0
    stopped_by: str = "done"            # done | budget | error
    error: str | None = None
    calls: list[dict] = field(default_factory=list)          # per model call: ticket, context size, usage, cost
    summaries: list[dict] = field(default_factory=list)      # compaction summaries, verbatim
    memory_files: dict[str, str] = field(default_factory=dict)
    memory_ops: list[dict] = field(default_factory=list)


def run_shift(shift: Shift, world: World, model: str, strategy: str, *, effort: str = "medium", max_usd: float = 5.0,
              max_calls_per_ticket: int = 12, limit: int | None = None, progress=print) -> RunResult:
    edits, betas, use_memory = STRATEGIES[strategy]
    client = anthropic.Anthropic(max_retries=6, timeout=300)
    memory = MemoryStore()
    tools = TOOL_SPECS + ([MEMORY_TOOL] if use_memory else [])
    req: dict = dict(
        model=model, max_tokens=8000, tools=tools,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        cache_control={"type": "ephemeral"}, output_config={"effort": effort},
    )
    if edits:
        req["context_management"] = {"edits": edits}
    if betas:
        req["betas"] = betas
    res, messages = RunResult(), []
    for t in shift.tickets[: limit or len(shift.tickets)]:
        world.begin_ticket(t.idx, t.id)
        messages.append({"role": "user", "content": (KICKOFF + "\n" if t.idx == 0 else "") + t.user_message()})
        for _ in range(max_calls_per_ticket):
            try:
                resp = client.beta.messages.create(messages=messages, **req)
            except anthropic.APIError as exc:
                res.stopped_by, res.error = "error", f"{type(exc).__name__}: {str(exc)[:400]}"
                break
            _account(res, resp, model, t.idx)
            if not resp.content:              # seen with tool-result clearing: an empty turn reported as tool_use
                res.calls[-1]["empty"] = True
                break
            messages.append({"role": "assistant", "content": resp.content})
            for b in resp.content:
                if b.type == "compaction":
                    res.compactions += 1
                    res.summaries.append({"ticket_idx": t.idx, "summary": b.content})
            for e in getattr(getattr(resp, "context_management", None), "applied_edits", None) or []:
                res.calls[-1]["cleared_tool_uses"] = getattr(e, "cleared_tool_uses", 0) or 0
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason != "tool_use":
                if resp.stop_reason == "refusal":
                    res.calls[-1]["refusal"] = True
                break
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                out = memory.handle(b.input, t.idx) if b.name == "memory" else world.call(b.name, dict(b.input))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            if not results:
                break
            messages.append({"role": "user", "content": results})
        if res.stopped_by == "error":
            break
        res.tickets_done = t.idx + 1
        if res.cost_usd > max_usd:
            res.stopped_by = "budget"
            break
        if (t.idx + 1) % 10 == 0:
            progress(f"    ticket {t.idx + 1}: calls={res.model_calls} context={res.calls[-1]['context']:,} "
                     f"compactions={res.compactions} cleared={res.cleared_tool_uses} cost=${res.cost_usd:.3f}")
    # cleared_tool_uses is reported cumulatively per request; a clear event is a call where the number went up
    prev = 0
    for c in res.calls:
        n = c.get("cleared_tool_uses", 0)
        if n > prev:
            res.clear_events += 1
        prev = max(prev, n)
    res.cleared_tool_uses = prev
    res.memory_files, res.memory_ops = memory.files, memory.ops
    return res


def _account(res: RunResult, resp, model: str, ticket_idx: int) -> None:
    """Add one response's usage. With compaction on, billing is the sum over usage.iterations, not the top-level numbers."""
    u = resp.usage
    its = getattr(u, "iterations", None) or [u]
    cost = 0.0
    for it in its:
        inp, out = it.input_tokens or 0, it.output_tokens or 0
        cw, cr = getattr(it, "cache_creation_input_tokens", 0) or 0, getattr(it, "cache_read_input_tokens", 0) or 0
        res.input_tokens += inp
        res.cache_write_tokens += cw
        res.cache_read_tokens += cr
        res.output_tokens += out
        cost += call_cost(model, inp, cw, cr, out)
    last = its[-1]
    context = (last.input_tokens or 0) + (getattr(last, "cache_creation_input_tokens", 0) or 0) + (getattr(last, "cache_read_input_tokens", 0) or 0)
    res.model_calls += 1
    res.cost_usd += cost
    res.peak_context = max(res.peak_context, context)
    res.calls.append({"ticket_idx": ticket_idx, "context": context, "output": last.output_tokens or 0, "cost": round(cost, 6),
                      "iterations": len(its), "stop": resp.stop_reason, "t": round(time.time(), 1)})


def dump(obj) -> str:
    return json.dumps(obj, indent=1, default=str, ensure_ascii=False)
