# Design

Why `jevskill` is built the way it is, including the mistakes that shaped it.

## The problem

A coding harness makes hundreds of small judgements per session: which module
owns a failure, whether a diff is safe, which log lines matter, whether a test
covers the bug, what a stack trace says. Doing these with a chat model means
paying frontier prices, waiting seconds, and parsing JSON that is occasionally
malformed. Doing them by hand costs human attention.

Jev is built exactly for this: a *System One* model that returns typed decisions
with calibrated probabilities, no text, no reasoning, output free.

So the interesting question is not "is Jev fast" — it is **"when is it worth
reaching for, and how do you know it worked?"** This repository is an answer to
that second question, and the answer is mostly measurement infrastructure.

## Layer map

```
skills/jev/SKILL.md          the decision guide a harness actually reads
        │
        ├── references/         api · patterns · prompting · benchmarks
        │
jevskill/cli.py         the command surface (doctor · plan · patterns ·
        │               ask · outcome · stats)
        ├── orchestrate.py      planning: pattern, size, iteration, savings
        ├── primitives.py       noul / choice / score + fail-fast validation
        ├── client.py           Decisions API: warm connection, retries, timings
        ├── stages.py           staged timing
        └── stats.py            the effectiveness ledger
```

The split matters: `client.py` knows the API, `orchestrate.py` knows the
*strategy*, and neither knows about the other. Swapping the provider would touch
one file.

## Design decisions worth defending

### 1. Publishing the measurement, not just the tool

A skill that helps you use a model is only as good as the reader's belief that it
works. So the ledger, the benchmark suite and the negative results ship with the
code:

* `bench/results.json` is generated, not written by hand.
* `bench/run.py --legacy-reduce` reproduces a design that **failed**, so the
  negative result is checkable.
* The README reports that a small chat model was faster and cheaper on a trivial
  call, because that is what the measurement said.

### 2. Absolute timestamps, derived deltas

The first version of `stages.py` recorded each stage as "elapsed minus the sum of
previous stages", and separately absorbed the client's own
`serialize`/`http`/`parse` timings. That double-counted: the doctor command once
reported `act: -612 ms` and a 1047 ms total for a 45 ms script.

The fix is structural. `Stages.mark()` stores the **absolute** elapsed time and
`ordered()` derives durations from consecutive marks, so the parts sum to the real
wall clock. Client-side timings are attached as an **overlay** in parentheses —
visible, but never summed, because they sit *inside* an already-measured region.

### 3. Validation before the network

The documented 400 causes are almost always a `choice` with one option or an
empty `criteria`. `primitives.py` refuses to build those, and
`validate_questions()` re-checks bundles that arrived as JSON, so a malformed
question costs zero round trips and zero cents.

### 4. A schema-stable ledger

Every row carries the same keys because `Record` defines them, including
`outcome: ""` for decisions not yet paired. A reader can therefore consume any row
without probing which fields exist. Outcomes are appended as separate patch
records rather than rewriting the decision line, which keeps the ledger
append-only and safe for concurrent writers.

### 5. Errors that tell you what to do

`JevApiError` maps each documented status to the action a harness should take —
413 means "chunk it", 402 means "fall back to the LLM path", 404 means "you used
the chat endpoint". A bare status code makes a model guess.

## The bug that shaped the prompting rules

The REDUCE pattern was implemented as: score each 60-line chunk, keep the three
hottest, gate the lines inside. It reported a **99.1% context reduction while
keeping 1 of 8** wanted lines. Adding a line-level second stage made it *worse* —
0 of 8 — and the reason turned out to be the most valuable finding in the
project.

Measured on one 20-line window containing a single real `ERROR`:

| Variant | Real `ERROR` | Noise line | Useful |
|---|---|---|---|
| A: `{"lines": {L0…L19}}`, question says "this single log line" | 0.72 | 0.75 | ❌ |
| B: one line per state, one call per line | 0.98 | 0.02 | ✅ but 20 calls |
| C: per-line keys, question points at `` `L0` `` | **0.96** | **0.03** | ✅ batched |

Variant A fails **silently**: the numbers look like answers. Naming the target
with a backticked path — a documented TypeSafe convention this project had read
and not applied — restores discrimination at no cost.

Two consequences, both now enforced:

* `references/prompting.md` opens with this rule and the table above.
* `bench/run.py` keeps the failing single-stage design as
  `--legacy-reduce`, so the lesson is reproducible rather than folklore.

## Windows notes

A harness runs in whichever shell a human opened. `setx` changes are **not**
inherited by processes already running, which is the most common reason an API key
"disappears". `config.py` therefore reads `HKCU\Environment` directly with
`winreg` after the environment variables come up empty, so a key set yesterday
works in a terminal opened before it.

The registry is only consulted on `os.name == "nt"` and every failure is
swallowed into an empty string, so the fallback can never break a working setup.

## Why stdlib-only

`httpx[http2]` and `orjson` are meaningfully faster, and the client uses them when
they are importable. But a harness shelling out to a tool should not fail because
a virtualenv lacks a dependency, so `urllib` and `json` are the fallback and
`pip install -e .` has no requirements at all. The measured penalty is inside the
noise, because `http` is 92–96% of wall clock regardless.

## What is deliberately not here

* **No MCP server.** A CLI keeps the integration surface small and lets any
  harness — Claude Code, DSH, a shell script — use it identically.
* **No caching.** Jev is deterministic per state, but caching decisions would make
  the ledger lie about what was actually measured.
* **No attempt to outperform an LLM.** The README says so explicitly. Jev wins on
  structure, latency and fan-out; on raw accuracy against a frontier model it does
  not, and pretending otherwise would discredit the parts that are true.

## ADR — `SKILL.md` is an instruction set, not a report (2026-09-20, 0.12.0)

**Context.** At 0.11.0 `SKILL.md` was 499 lines against a documented ceiling of 500,
and roughly two fifths of it addressed a *human* deciding whether this skill was
worth keeping: a ledger walkthrough, an `advice` verdict table, a per-stage timing
breakdown, a provider-comparison table, and an opening that argued the skill's own
credibility ("Measured, not estimated"). An Agent Skill's body is loaded in full the
moment the skill activates, so every one of those lines was spent from the same
context window the actual instructions need — and none of them changed what an agent
would do next. The critique is written up in
[research-2026-09-20-jev-cu.md](research-2026-09-20-jev-cu.md) §2.3.

**Decision.** The body is capped at **250 lines** and every paragraph has to earn
its place by changing an agent's next action. Seven sections, in the order an agent
needs them: *is this the right tool* → *the three primitives* → *how to call it* →
*the four rules* → *how to read the answer* → *key and provider* → *constraints and
the router*. There is now **one** default call path (the zero-install script, with
`$SKILL_DIR` explained) instead of two competing ones, because choosing between
them cost a turn. Measurement prose moved verbatim to
[`references/measure.md`](../skills/jev/references/measure.md); provider tables were
already in `api.md`.

**Consequences.**
- Figures are almost entirely gone from the body. The two fan-out ratios stay
  (`12.4×`, `4.03×`) because `TestPublishedFiguresAreMeasured` requires them as the
  standing proof that the 9.4× defect cannot return, and one cost figure stays
  because "what does a decision cost?" changes whether an agent fans out or loops.
  Every duration now lives in `benchmarks.md`, which publishes its method alongside.
- The ceiling is enforced by `TestSkillIsADecisionGuide`, not by a sentence. The
  previous ceiling *was* only a sentence, and the file sat one line under it.
- A reference that no router row names now fails a test, so `references/` cannot
  accumulate files nobody is routed to.
- `allowed-tools` and the body are checked against each other: the field used to
  say `Bash(python:*)` while every example was `jevskill …`.
- The cost is indirection: an agent that wants the ledger must open a second file.
  That is the correct trade for a document loaded on every activation.

## ADR — `hot` is a client flag, not a `Config` mode (2026-09-20, 0.12.0)

**Decision.** `JevClient(hot=True)` retunes the read timeout, connect timeout and
retry count for a per-step agent loop. There is no `Config.hot()`.

**Why.** A `Config` answers *where do I send this and with which key*. It is
resolved once from the environment, shared by the CLI and by every client in a
process, and may be handed around. Hot is not a fact about the endpoint; it is the
call pattern of **one** client. Putting it on the constructor keeps a loop from
retuning a config that a burst-mode caller is also using, and it makes the
default path byte-identical to what it was: no extra thread, no extra field on the
result, unless the caller asked. The knobs still live on `Config` (`hedge`,
`hedge_after_ms`, `warm_mode`) so `doctor` can read them back and a caller can
state them explicitly; hot only supplies different *defaults*.

## ADR — hedging ships off, because it lost (2026-09-20, 0.12.0)

**Decision.** A duplicate request after `hedge_after_ms` is implemented, tested,
and **disabled by default** on every path including the hot one.

**Why.** The plan expected a hedge to cover the tail once retries were zero. It was
measured before being believed (`bench/cu_results.json`, 160 hedged calls on both
providers plus a 320 ms probe): 66 duplicates fired and **none won**, and the calls
that fired one were slower. Both legs share one multiplexed HTTP/2 connection to one
provider, so the twin cannot escape a slow connection and makes the server do the
work twice; it also starts late, so it can only beat a primary that is *stuck*, and
the tail at large states is a property of the request, not noise. The code stays
because the stuck case is real and unrepresented in the sample, and because a hedge
over a second connection or a second provider is an experiment nobody has run. The
abandoned leg is always costed — recording it as free would poison the ledger this
project uses to claim savings.

## ADR — perception reads the UIA tree through one `CacheRequest` (2026-09-20, 0.12.0)

**Decision.** `jevskill.cu.observe` uses `comtypes` and a single
`BuildUpdatedCache(TreeScope_Subtree)` per window; `uiautomation` and `pywinauto`
are used only by the comparison bench.

**Why.** Measured, three warm runs per app (`bench/cu_observe_results.json`): the
two libraries read every property live — one cross-process call per node per
property — and took 138–223 ms on a 34-node Notepad; one `CacheRequest` fetching
sixteen properties for the whole window took 73–104 ms, and 42–52 ms on a 53-node
Calculator. The cost that remains is the provider's and tracks its node count
(~1 ms per node; a Notepad with 18 restored tabs cost 368–410 ms), and no
client-side trick moved it. A lazy per-container descent tied on an idle machine
and lost under load, so `strategy="subtree"` is the default and `"lazy"` is kept
only for windows too large to fetch at once.

**The split this enables.** Visible, enabled, on-screen, duplicated, *unchanged* —
these are facts, and code settles facts faster and more reliably than a model: the
`stuck` question the model was asked instead returned 0.42–0.60 on screens that had
plainly changed. Everything a step can know without judgement is computed in
`reduce`/`hashing`; the model is asked only which of the remaining controls the
goal wants.

## ADR — the planning model sits *between* Jev steps, never instead of them (2026-09-21, 0.14.0)

**Decision.** `jevskill.cu.runner.Operator` lets the user pick any OpenRouter
model, and calls it for exactly five things: splitting a command into sub-goals,
composing the text a field needs, judging a sub-goal's `done_when`, answering an
escalation, and re-planning when a sub-goal ends without `done` (at most twice
per run). Every per-step decision — which control, which op, is it destructive,
is the goal reached — stays with Jev through the unchanged `jevskill.cu.loop.run`.

**Why.** The loop is fast because the model in it answers typed questions in
~300 ms and is never asked to write. The things it cannot do are all *language*:
reading a Polish sentence, producing "hello world", deciding that "saved as
hello.txt" holds. A frontier model doing the whole step costs seconds and
dollars per step (`docs/research-2026-09-20-jev-cu.md`: 5.2 s vs 0.13-0.38 s); a
frontier model doing only the five language jobs costs a handful of calls per
run. The split keeps the per-step budget and buys the understanding. Its cost is
visible: each call is a ledger row (`which="cu_llm"`) next to the loop's `act`
rows, so `jevskill stats` reports what a command actually cost, both halves.

**The escalation answer is still validated.** A model's proposal goes through
`validate()` and the destructive gate like a Jev decision — the loop's own rule
from 0.12.0 (`_escalate`), which this module relies on rather than repeats.

## ADR — the kill switch lives in the hooks, so the loop stays unchanged (2026-09-21, 0.14.0)

**Decision.** Stopping is implemented as a `KillSwitch` checked inside the
`observe` and `execute` callables the operator injects, not as a new parameter
of `run()`. A tripped switch raises `Stopped`; the loop already treats any
exception as the end of the run and records the name; the operator maps that
name to *stopped*.

**Why.** Two checks per step are the two moments that matter: before spending a
decision, and — after the 300 ms the decision took — before the action reaches
the desktop. Putting them in the hooks covers every path through the loop
(ordinary step, macro replay, escalation answer) without a third copy of the
check and without touching a module whose 0.12.0 behaviour is pinned by 200
tests. Four triggers feed one event: the panel's STOP, `Ctrl+Alt+Esc` polled with
`GetAsyncKeyState` (no message loop, no hotkey collision, works over a
full-screen window), the cursor in the top-left corner, and a stop file that
`jevskill cu stop` touches so a second terminal, SSH or a scheduled task can end
a run that the browser started. The switch is armed only while a run is active,
so a mouse parked in the corner between runs does nothing.

**Measured, then amended (2026-09-21, live).** The first live runs happened
with the user working on the same desktop, and the switch was not what ended
most of them: the operator was. It observed "the foreground window" and, when
the user switched away, pulled its window back before every observation and
action — three times in 40 s while the user typed in Discord, until Windows
itself refused. A first amendment, *bring it back once, stop on the second
loss*, ended the next run because the user was reading the console. The rule
that held is: **reading needs no screen, keys do.** The pinned window is
observed by handle (`GetLastActivePopup` finds a dialog it opened), in front or
not; only synthetic input — a key chord, text into a control without a
`ValuePattern`, a click by point, the wheel — takes the foreground, after
waiting up to 10 s for the user's hands to pause (`GetLastInputInfo`), and the
switch to a freshly launched window waits the same way, because the user's
next two keystrokes once landed in it. The corner trigger fired once, 1.5 s
after a launch, as the user's cursor crossed the corner: the convention working
as documented, and it stays a hair trigger — a dwell requirement would not tell
a parked mouse from a fling to the first browser tab, and the cost of a false
stop is one re-run.

## ADR — the operator remembers, and code verifies before a model does (2026-09-27, 0.15.0)

**Decision.** `jevskill.cu.experience` keeps four kinds of knowledge on disk
(next to the ledger when a root is named, else `~/.jevskill/cu_experience.json`),
and the operator consults it before every model call it would otherwise make:
**plans** (command template → the goals that completed it), **recipes**
((application, goal template) → the actions that completed the goal, by control
identity), **lessons** (actions that changed nothing or were refused, with what
worked instead) and the **end state** a goal was verified in. A remembered plan
skips the planning call; a recipe replays the goal with no Jev call and no model
call; a recipe answers an escalation before the model is asked; the screen is
checked in code — the recipe's end state, then the quoted values of `done_when`
— before the model is asked to verify or to re-plan.

**Why.** The first live save (`bench/cu_live_save_run.json`) took 60.1 s. Jev
decided in about 5 s of it; twelve planning-model calls took about 33 s; waiting
for the user's hands to pause took 10 s. Every one of the twelve answered a
question an earlier attempt had already answered, and nothing was kept, so the
next run would have paid the same 33 s. The model is the supporting module: it
is asked what the operator does not know yet, once.

**Templates, not strings.** Quoted text and file names are cut into slots and
put back from the current command, so a plan learned for `„hello world”` /
`hello.txt` serves `„lista”` / `zakupy.txt`; a command that differs in any other
word misses and is planned fresh. A composed text with no slot in it (a path the
model wrote) replays only under the command template it was learned under —
the same goal from another command re-composes it. A miss costs one model call;
a false hit types the wrong thing into someone's document.

**Every replayed step is still checked.** The control must resolve on the
current screen (identity, then automation id, then a *unique* name — identical
twins are refused, a test caught the first version picking one), the action
goes through the operator's execute hook (kill switch, foreground rules), a
destructive control still asks, and the screen must change within the settle
window (2 s for a step that opened a window). The goal counts as done only when
the screen matches how it ended before, or the verifier says so. A recipe that
fails twice in a row is not offered until a run completes the goal the ordinary
way and relearns it. A step-less recipe is kept only for a goal whose launch did
the work: otherwise "type the text" after a goal that already typed it would
replay as "nothing to do".

**Verification in code is conservative.** A `done_when` that names the title is
checked against the title only; otherwise quoted values must sit in an editor
with no dialog up — while a Save As dialog is open, `hello.txt` in its file-name
field says nothing about whether the file was saved. Code answers *yes* or
*cannot tell*, never *no*.

**Only a real desktop teaches.** A dry run reads memory and writes nothing;
`memory=False` (panel switch, `--no-memory`) neither reads nor writes. A
"forget" writes a tombstone into the file, so another process's stale copy
cannot write the entry back — the macro cache's in-process tombstones could,
and a test here showed it.

**Non-invasive, amended.** With the window read by handle, a launched window no
longer needs the foreground at all: it is found (a new top-level window, or the
program's existing window once its title shows the new tab) and pinned, and
nothing is switched. The window the user had in front before a key chord took
it is given back when the run ends, if the agent's window still holds it. The
planning and escalation prompts prefer controls and menus — UI Automation
patterns that act on a background window — over chords.

**Measured so far: offline only.** `tests/test_cu_memory.py` drives the real
operator over a simulated Notepad and Save As dialog (`tests/cu_sim.py`): the
first run of the save command makes two model calls (plan, compose) and four
loop runs; the second makes none of either and saves the same file; a third
with new values types the new values. No latency is claimed from the
simulation; the live effect is unmeasured until a live run.

## ADR — the plan is a tree; only leaves act; recovery repairs the scope, not the plan (2026-09-27, 0.15.0)

**Decision.** A command is planned as a tree: the root (the command), up to 12
**phases** (one milestone in one program, with its own `done_when` and optional
code checks), and **leaves** — the one-window goals `loop.run` has always
driven. The General Agent Plan is the old planning prompt with phases appended
after the same rules, so a short command makes the same call and gets the same
all-leaf answer; `hierarchical=False` sends the old prompt byte for byte. Only
the first phase comes back broken down; the others are broken down when the
cursor reaches them, from the screen the previous goal left. A failure is
repaired in its **scope** — the failed item's parent — and the level above is
asked only when that scope cannot recover. The whole design came out of a
judge panel of three independent designs (an agenda tree, a mission tree with
a journal, a supervised milestone plan); the agenda tree won and took the
others' journal, resume rules, launch-through-the-hook and quote-checked
verdicts.

**Why lazy.** A phase planned up front is planned against a screen that does not
exist yet — the Save As dialog of phase 3 cannot be described before phase 2
has typed anything. Broken down when reached, it sees the real screen, and the
expansion reuses the previous goal's observation, so it costs one model call
and no extra UI walk. A phase broken down before, in any mission and with any
file name, costs none: decompositions are remembered under the phase's goal
template (`phase\x1f` keys inside the existing `plans` section — an older build
merges that dict wholesale and keeps them).

**Why repair the scope.** The flat run's re-plan rewrote everything after the
failed goal. In a 40-goal mission that throws away phases that were fine and
buys a planning call sized for the whole command. A scope repair sees its own
children, the failed goal with what was `tried`, and the screen; it may finish
(only with a quote that is on the screen), revise the scope's remaining work,
skip an item marked optional *before* it failed, ask the user, give up, or send
the question up one level. Bounds keep a bad model from looping: two repairs per
scope (the flat run's `MAX_REPLANS`, so a flat run behaves as before), eight per
run, a goal that failed twice is filtered out of any proposal, 60 goals, depth
3, and an LLM-call allowance of 40 plus 6 per phase, at most 200.

**Only leaves act.** Planning, expansion, repair, phase acceptance, pinning,
pause and resume only read. `_GuardedBackend` makes a backend call from outside
`Operator._execute_hook` a `RuntimeError`, and a launch now goes through the
hook too — the kill switch is checked right before it, which an allow-listed
launch used to skip. A test records every backend call of a learned run and a
replayed one and finds none outside the hook.

**Resume is explicit and careful.** Every run checkpoints to
`cu_runs/<run_id>/run.json` (atomic replace) and appends `events.jsonl`; the
first action a goal executes forces a checkpoint, because that is the moment a
blind rerun stops being safe. A resumed goal that had acted is not rerun: code
checks the screen first, then the model is told it was interrupted after N
actions, or — Jev only — the user is asked. A run with a fresh heartbeat is
refused (it may be alive in another console), a finished one is refused, and a
dry run resumes dry. The heartbeat is its own file, written by a timer thread:
the run thread blocks for up to a minute in a model call, past the 30 s
staleness mark, and a live run used to look dead. Run ids arrive over HTTP and
are matched against a strict pattern before any path is built.

**A goal works only in a window of the program it names.** A phase in another
program pins that program's window once; with several windows of it, the one
this run already worked in, or the only one — never a guess among the user's
documents. With no window, an allow-listed program is opened through the
execute hook and anything else waits for the user. The window pinned before is
never borrowed: the first version of the tree did, and the simulator recorded a
Calculator goal typing into Notepad. A resume whose window closed takes only a
window with the saved title.

**Rejected.** Look-ahead expansion in a background thread (a prediction of a
screen, plus locks around spend and events, for a latency nobody has measured);
skipping a phase by code before expanding it (a title substring is true for an
unsaved `*hello.txt`, `file_exists` for yesterday's file); per-node dollar and
call budgets (the loop swallows hook exceptions, so the only reliable cap is at
leaf boundaries and at the next observation); approval checkpoints on the plan
or on evidence (time to success matters most; the per-action destructive gate
stays).

**Measured so far: offline only.** `tests/test_cu_mission.py` drives the real
operator over the simulated desktop: a three-phase mission makes one planning
call and one break-down per later phase; a failed goal is repaired inside its
phase without replanning the mission; a remembered two-phase mission replays
with no model call and no loop run and saves the file with the new values. No
latency is claimed until a live multi-phase run writes a reproducible JSON.
