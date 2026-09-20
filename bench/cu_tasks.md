# Windows CU benchmark — method

Task list: [`cu_tasks.json`](cu_tasks.json) (10 tasks). Context: `docs/plan-2026-09-20.md`
Phase 4 item 4.7 (T4.7 in `TASKS.md`), `docs/research-2026-09-20-jev-cu.md` §4 (who else has
built this) and §5 (where GUI-agent time actually goes). This file is the method only — it does
not itself contain measured numbers; those go in `bench/cu_task_results.json` once a runner exists
(`cu/loop.py`, Phase 4.1–4.6) and get reported alongside this table, never in place of it.

## Method

- 10 tasks × 3 runs each, per agent under test = 30 timed runs. Each run: `setup` from a clean
  state, hand the agent the `goal` string, start the clock; poll the task's `oracle`
  independently of the agent; stop the clock the instant the oracle is first true; run `teardown`
  unconditionally, including on timeout or crash. Recommended per-run cap: 60 s or the loop's own
  step budget (`cu/loop.py`, Phase 4.5), whichever the runner enforces — record which.
- Run order across the 10 tasks should be randomized per repeat (not always 1→10) so a slow first
  call (`warm()`, cold caches) does not land on the same task every time.
- `settings_dark_mode` changes the real desktop theme on this host; run it in a slot where that is
  acceptable (e.g. last), not silently in the middle of an unrelated recording.

## Metrics per run

| metric | meaning |
|---|---|
| `wall_ms` | goal handed to agent → oracle first true. Not agent-reported completion time. |
| `jev_calls` (or model calls) | number of decision round-trips the loop made; the comparison agent may call a different model — record calls, not "Jev calls", generically |
| `input_tokens` | summed prompt/state tokens across those calls, if the agent under test exposes them |
| `cost_usd` | summed from provider pricing; use `usage.cost_source` (`computed`/`provider`) per `jevskill` convention — never silently record 0 for a provider that omits cost |
| `escalations` | times the loop fell back to OCR/VLM/human per Phase 4.8, or the comparison tool's equivalent |
| `success` | boolean, from the oracle only (see below) |

Also log step count (physical UI actions) separately from `jev_calls` when they can differ (e.g.
a cached macro hit per Phase 4.6 spends 0 calls but still 1 step).

## Success is judged by the oracle only

The agent's own `done`, `goal_reached`, or exit message is never the success signal — only
`cu_tasks.json`'s `oracle` for that task, evaluated by the harness after the agent stops (or times
out). Two independent reasons this repo already has evidence for:

1. jev-ultrafast's own note: "A `DONE` choice still requires independent outcome verification"
   (research doc §4) — even the fastest known Jev-based CU agent does not trust its own done signal.
2. This repo's own measurement of the `stuck` question (research doc §3): 0.42–0.60 on states that
   had *obviously* changed — a Noul asked to compare two states is not reliable, so a Noul asked
   "did I finish?" gets no more trust here than that.

If the oracle cannot be evaluated (crash mid-teardown, ambiguous state), record `success: false` and
a `reason`, never a retro-fit pass.

## Threats to validity

- **n = 3 is not a reliability benchmark** — it bounds obvious flakiness, not tail behavior. Report
  all 3 raw values per cell, not just a mean, and say so next to the table (same framing the
  research doc uses against jev-ultrafast's own 3-repeat numbers).
- **One machine, one point in time.** Network path affects latency (OpenRouter vs `api.typesafe.ai`
  differed by 30–60 ms in this repo's own bench, §3) — pin and record which provider/route, or the
  numbers are not comparable across sessions.
- **Polish-locale Windows.** Control names (menu items, button labels) are Polish; goals and oracles
  in `cu_tasks.json` are deliberately worded and graded to not depend on that, but this is one
  locale only — no coverage of RTL, CJK, or a second Windows display-language build.
- **UIA coverage is app-specific, not a Windows-wide constant** — macOS AX coverage in the same
  research doc ranged 100% (Finder) to 0% (Spotify); the 5 apps chosen here (Notepad, Explorer,
  Settings, Chrome, Calculator) are all comparatively well-covered WinUI/Win32 apps by design, so
  this benchmark is optimistic about UIA coverage relative to the general app population.
- **First-run and persisted-state dialogs** — Notepad's tab restore, Explorer's per-content-type
  view memory, Chrome's first-run prompts. Flagged per task in `cu_tasks.json`'s `notes`; a run
  that silently eats one of these as an extra unplanned step should be logged, not averaged away.
- **This host is being live-streamed.** Chrome tasks use an isolated `--user-data-dir` specifically
  so the benchmark never touches the streamer's real browser session; `settings_dark_mode` cannot
  be made invisible the same way (see its `notes`) — schedule accordingly.

## Running the same list on `kofanlabs/typesafe-computer-use-windows`

- Its CLI accepts a single goal string. Feed it exactly the `goal` field from each task in
  `cu_tasks.json`, one at a time; reuse this repo's own `setup`/`teardown` scripts unchanged — they
  drive the OS, not the agent, so they are agent-agnostic.
- It also needs a text/vision model wired in for its OCR+AX decision step (research doc §4) — pin
  and record which model and version, the same way the provider is pinned for Jev above. A
  comparison across two different backing models is not a comparison of the *architectures*; say
  which variable moved.
- Grade its runs with the **same** `cu_tasks.json` oracles, not its own reported success — the
  "oracle only" rule above applies equally to the comparison tool, otherwise the two columns are
  not measuring the same thing.
- Its step/log format differs from this repo's per-step JSON (Phase 4.5); normalize both into the
  results table below rather than quoting each tool's native report format.

## Results table skeleton

Per task, 3 runs per agent, raw values plus one aggregate line:

| task | agent | run | wall_ms | calls | input_tokens | cost_usd | escalations | success |
|---|---|---|---:|---:|---:|---:|---:|---|
| notepad_save_as | jev-cu | 1 | | | | | | |
| notepad_save_as | jev-cu | 2 | | | | | | |
| notepad_save_as | jev-cu | 3 | | | | | | |
| notepad_save_as | jev-cu | median | | | | | | n/3 |
| notepad_save_as | typesafe-computer-use-windows | 1–3, median | | | | | | n/3 |
| … | | | | | | | | |

Final summary: one row per agent, totals across all 10 tasks (sum of `wall_ms`/`cost_usd`, mean
`calls`, overall success rate out of 30), plus escalation rate, plus which tasks (if any) that
agent failed on all 3 runs — a 0/3 task is a finding, keep it, do not drop it from the table.

## Running it

The harness is `bench/cu_run.py` (runs and grades) plus `bench/cu_report.py` (renders
the tables above from what the runner wrote). Both are stdlib-only and repo-relative,
like `bench/cu_bench.py`.

```bash
python bench/cu_run.py --dry-run          # the default: touches nothing
python bench/cu_report.py                 # the table, marked DRY RUN

python bench/cu_run.py --live --i-am-not-streaming --agent jevskill.cu.loop:run --runs 3
python bench/cu_report.py --compare bench/cu_runs_kofanlabs.json --label jev-cu --compare-label kofanlabs
```

**`--dry-run` is the default and `--live` needs `--i-am-not-streaming`.** A live run
opens applications, types into them, takes the foreground, and for
`settings_dark_mode` flips this machine's theme while it is in progress. No
environment variable and no config file can supply that flag; the one assertion that
must not be automatable is "nobody is being filmed right now". `make_powershell_phase`
— the only code that executes a task's `setup`/`teardown` — refuses to be constructed
until the flag has armed it.

A dry run does four things, none of which touch the desktop: it validates the schema of
`cu_tasks.json`, parses **every** `setup`/`teardown` snippet through
`[System.Management.Automation.Language.Parser]::ParseInput` (which builds a syntax tree
and never executes what it is given — one `pwsh` process for all 42 snippets, skipped
with a message when `pwsh` is absent), runs a deterministic fake agent, and grades it
with a fake oracle that always passes. Every row it writes carries `"mode": "dry-run"`
and `"synthetic": true`, and the report prints DRY RUN in the title of every table built
from such rows. Synthetic numbers are useful for checking that the columns are wired to
the right fields; they are evidence about nothing.

### The contract

`jevskill/cu/contract.py` is what the harness and every agent share — `StepRecord`,
`RunResult`, `RunOptions`, and one callable, `run(goal: str, opts: RunOptions) ->
RunResult`. **`RunResult` has no `success` field and must not grow one**: success is
the oracle's verdict, carried on `TaskRun`, which only the harness builds. `summarise`,
`escalation_rate` and `escalations_per_task` are pure functions of a list of results, so
the report is testable without a desktop.

### Where the implementation departs from the method above, and why

| Method says | Harness does | Why |
|---|---|---|
| poll the oracle independently, stop the clock the instant it is first true | evaluates the oracle once, after the agent returns | A concurrent poller would need a thread per run and would still not see a state the agent has already torn down. Both clocks are recorded instead: `wall_ms` is the agent's own, `harness_wall_ms` is goal-handed-over to agent-returned. When the two disagree, the gap is the agent's book-keeping error and is visible rather than hidden. |
| per-run cap of 60 s or the loop's step budget | `RunOptions(max_steps=25, budget_s=90.0)`, reported not enforced | The harness cannot preempt a Python callable. An overrun is recorded as a note on the row; the agent is expected to stop itself. |
| randomize run order per repeat | shuffled per repeat (`--seed`, default 0), then `restores_state` tasks forced last | Both rules from this file at once: a cold first call should not always land on the same task, and `settings_dark_mode` should never land in the middle of a sequence someone is recording. |
| oracles as written in `cu_tasks.json` | `file_exists`, `file_contains`, `all_of`/`any_of` and `registry_value` are graded in Python; `uia_value`, `window_title_contains` and `clipboard_equals` raise `OracleUnsupported` | Those three need a live accessibility/window reader, which is `cu/observe.py` (Phase 4.1). Until it lands, five of the ten tasks — `calc_multiply`, `calc_scientific_power`, `chrome_find_continue`, `chrome_open_url`, `explorer_view_details` — cannot be graded, and an ungraded run is recorded as a failure with a reason, per "Success is judged by the oracle only" above. It is never back-filled from how the agent stopped. The other five — `explorer_new_folder`, `explorer_rename`, `notepad_replace`, `notepad_save_as`, `settings_dark_mode` — are graded by filesystem and registry reads alone. `tests/test_cu_run.py` pins both lists, so this sentence cannot drift from the code. |
| results in `cu_task_results.json` (plan item 4.7) | `bench/cu_runs.json` | One file for both agents, keyed by `(task_id, run, mode, provider)` and merged rather than truncated, so two agents and two providers are four invocations into one table. The dry run's output is not committed: it is synthetic, and nothing quotes it. |

### Escalation accounting (Phase 4.8)

Counted two ways, because they are two different failures. `decided_by == "escalation"`
on a step is a step handed to a VLM or a human mid-run; `stop_reason == "escalated"` is
a run that ended there. `escalation_rate` is the **step** figure — plan item 4.8's
"odsetek kroków eskalowanych" — and `escalated_run_rate` is the run figure. The report
prints both on every table, including when they are zero, and lists escalations per
task; a rate that only appears when it is non-zero is a rate nobody checks. If an
agent's self-reported `escalations` disagrees with the number of steps that say
`decided_by: "escalation"`, the mismatch is printed as an accounting problem rather than
averaged in.

### Order of phases, and what is guaranteed

`run_task(task, agent, setup, oracle, teardown, opts)` takes its four side-effecting
collaborators as arguments, which is how the ordering guarantees are tested with fakes
rather than with a desktop:

- **Teardown always runs** — after a clean run, after an agent that raised, after a
  setup that failed, after an oracle that blew up.
- **The oracle is consulted even when the agent errored.** An agent can reach the goal
  and crash on the way out; grading that as a failure because of how the agent felt
  about it is the same mistake as trusting its `done`.
- **A failed setup means the agent never runs** — the run did not start from a known
  state, so anything measured after it is about some other state.
- **The agent does not name its own cell**: `task_id` and `run` are overwritten by the
  harness's own loop.
