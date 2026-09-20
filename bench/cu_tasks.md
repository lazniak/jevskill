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
