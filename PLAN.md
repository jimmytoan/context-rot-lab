# Plan for the full run (not started, waiting for approval)

## What the pilot showed

All pilot runs used Claude Haiku 5.5, effort `medium`, one trial each, seed 0. Total pilot spend: **$1.72** (cap was $2).

| round | task | strategy | tickets | tasks ok | rule violations | what broke | compactions / clears | peak context | cost | minutes |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 12 rules, small lookups | full | 60 | 57/60 | 0/75 | nothing | 0 / 0 | 89,917 | $0.11 | 5.2 |
| 1 | 12 rules, small lookups | compact | 60 | 55/60 | 0/75 | nothing | 1 / 0 | 49,102 | $0.08 | 5.0 |
| 2 | 20 rules, doubled lookups | compact | 100 | 94/100 | 1/136 | re-ran a failed migration at ticket 96 | 5 / 0 | 49,472 | $0.16 | 9.3 |
| 2 | 20 rules, doubled lookups | clear | 100 | 94/100 | 2/136 | enterprise note skipped at 29 and 73 | 0 / 3 | 81,613 | $0.18 | 6.2 |
| 2 | 20 rules, doubled lookups | memory+clear | 100 | 70/100 | 2/136 | enterprise note skipped at 29 and 73 | 0 / 6 | 112,695 | $0.48 | 10.5 |
| 3 | final task | compact | 120 | 120/120 | 3/157 | re-ran a failed migration at 56, 85 and 115 (3 of 3 probes) | 8 / 0 | 49,490 | $0.21 | 13.6 |
| 3 | final task | clear | 94 of 120 (hit its $0.40 cap) | 91/94 | 2/114 | enterprise note skipped at 35 and 87 | 0 / 9 | 113,588 | $0.41 | 9.1 |

What this says, with one trial per row so treat it as direction only:

- Round 1 was too easy, the same trap as lab 2: zero violations, and the first compaction summary copied all twelve rules out verbatim.
- After hardening, losses appear and they differ by strategy. Compaction kept every stated rule but lost a **fact it had
  learned from a tool result** (which migrations failed), so it repeated the failed work. Tool-result clearing lost
  knowledge that lived only in cleared lookups (which customer is enterprise tier).
- No stated rule from the system prompt, briefing or notices was broken by any strategy in the pilot.
- Round 2 task failures were mostly my ticket wording (refund amount versus a four-item order made the agent ask for
  proof instead of refunding). Fixed before round 3, where compaction completed 120/120.
- Clearing does not hold context at the trigger: only tool results are cleared, so the conversation still passed 100K
  tokens, which is where Haiku 5.5 moves to its higher price tier. Clearing cost twice what compaction did.
- `full` and `memory` have not been run on the final task. Their costs below are estimates, not measurements.
- The clearing strategies also exposed a harness bug (an empty model turn reported as `tool_use`), fixed after round 1.

## Run matrix

Same shift for every run: 120 tickets, seed 0, 20 rules, triggers at 50,000 tokens, effort `medium`, concurrency 1.
Trials differ only by model randomness.

| strategy | Haiku 5.5 cost/run | Sonnet 5.5 cost/run | basis |
|---|---|---|---|
| full | ~$3.40 | ~$8.80 | estimated from ~380K peak context, ~350 calls |
| compact | $0.21 | ~$3.70 | Haiku measured, Sonnet scaled by price |
| clear | ~$0.55 | ~$5.70 | Haiku measured to ticket 94 and extrapolated |
| memory | ~$4.00 | ~$10.00 | estimated: full plus memory writes |
| memory+clear | ~$0.70 | ~$6.50 | from round 2, scaled |
| **one trial of all five** | **~$8.90** | **~$35** | |

The estimated rows could be off by 40% either way. The first step of any option is one Haiku `full` run under a
$5 per-run cap to replace the estimate with a measurement.

Three sizes:

| option | runs | estimated API cost | laptop time |
|---|---|---|---|
| **S. Haiku only** — 5 strategies x 3 trials on Haiku 5.5 | 15 | ~$27 | ~4 h |
| **M. Recommended** — S, plus Sonnet 5.5: compact, clear, memory+clear x 2 trials and full x 1 | 22 | ~$68 | ~7 h |
| **L. Full grid** — 5 strategies x 3 trials on both models | 30 | ~$130 | ~11 h |

Why M: the cheap strategies are where the pilot found differences, so they get repeated trials on both models. The
expensive `full` arm is the baseline everyone asks about, so it gets three Haiku trials and one Sonnet trial. Sonnet
`memory` alone (full context plus notes) is the costliest and least informative cell, so M leaves it out.

Commands for M (each batch has its own spend cap and can be resumed):

```bash
uv run python -m lab.run --model claude-haiku-5-5 --strategies full,compact,clear,memory,memory+clear --repeat 3 --tag main --resume --max-usd 6 --max-total-usd 35
```

```bash
uv run python -m lab.run --model claude-sonnet-5-5 --strategies compact,clear,memory+clear --repeat 2 --tag main --resume --max-usd 9 --max-total-usd 36
```

```bash
uv run python -m lab.run --model claude-sonnet-5-5 --strategies full --tag main --resume --max-usd 12 --max-total-usd 12
```

## Open choices

1. Which size (S, M or L), or a different budget.
2. Shift length. 120 tickets is what the pilot measured. 150 tickets matches the working title literally and costs
   roughly 40% more for compact and clear and 60% more for `full`.
3. Whether to add a cross-vendor cell later (OpenAI Responses API compaction, minimum threshold 1,000 tokens; or the
   Claude Agent SDK's own auto-compaction, window floor 100K). Not in this estimate.
4. Where to push. The repo has no remote and nothing is committed yet.

## Known weak spots to keep in mind when reading results

- 3 to 7 probes per rule per run. Per-channel and per-strategy totals are the numbers to trust; a single rule needs all trials pooled.
- The pilot tables under tags `pilot` and `pilot2` in `results/runs.jsonl` come from earlier versions of the task and
  are not comparable with `pilot3` or the main run.
- For a 100-ticket shift the late probes crowd together; use 120 or more.
