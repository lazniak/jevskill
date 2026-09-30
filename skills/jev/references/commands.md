# Command reference

Every command prints one JSON object with `--json` and a human report otherwise.
State comes from `--state`, `--state-file`, or stdin. Every command reports its own
stage timings, so the ledger's breakdown is comparable across commands.

## The package (`pip install -e .`)

```bash
jevskill doctor                # key, connectivity, warm latency, live cost
jevskill patterns              # the palette, with shapes and examples
jevskill plan "<problem>"      # FREE: should Jev be used? which pattern? how many calls?
jevskill ask --state-file diff.txt --question-type choice --name owner --options a b c unclear
jevskill ask --state ... --questions '<json>'      # a full bundle in one call
jevskill batch items.jsonl --text-key line --question-type choice --name owner --options a b unclear
jevskill outcome <decision_id> correct|incorrect|escalated|overridden|no_action
jevskill stats                 # measured latency, cost, savings, accuracy per pattern
jevskill advice                # what to do about it: KEEP / STOP / ESCALATE / UNPROVEN
jevskill web                   # the local console on http://127.0.0.1:8765
jevskill cu run "<command>" --model <id>   # drive this desktop: Jev per step, the model between
jevskill cu stop               # stop the active run, from any terminal
```

### `jevskill web` — the local console

A browser page for writing a question bundle by hand and reading the distribution
back: state with a live token estimate, question cards for the three primitives,
the patterns as templates, and answers drawn as bars with confidence, margin and a
`needs review` badge. Decisions land in the **same ledger** as `ask`, tagged
`which=web`, so `jevskill stats` counts them.

| Flag | Default | Effect |
|---|---|---|
| `--port N` | `8765` | port on 127.0.0.1; `0` picks a free one |
| `--no-open` | off | do not open the browser (the URL is still printed) |
| `--ledger-dir DIR` | — | directory holding `.jevskill/ledger.jsonl` |

**There is no `--host` flag, deliberately.** The console has no authentication and
spends a live API key, so it binds `127.0.0.1` only and `make_server` raises on any
other address. Forward the port over SSH if you need it from another machine. With
no key the page loads, names the variable to set, and disables Run — it never
simulates an answer.

The JSON API behind it is the same surface, for scripting: `GET /api/doctor`
(never key material — variable name, source and an 8-hex fingerprint),
`POST /api/estimate`, `POST /api/decide`, `POST /api/plan`, `GET /api/templates`,
`GET /api/history?limit=20`, `GET /healthz`. Every failure is
`{"error", "hint", "status"}`, with the `hint` a provider error carries.

### `jevskill cu` — computer use from a terminal

The operator behind the console's *computer use* view, without the page. A
natural-language command becomes 1–8 sub-goals; Jev decides every step; the
planning model (`--model`, any OpenRouter id) is consulted only between steps —
to plan, compose text, verify, escalate, re-plan. Omit `--model` and the command
is one goal with nothing generated. Events print as they happen; a destructive
step asks `y/N` on stdin.

```bash
jevskill cu run "Open Notepad and type \"hello\"" --model anthropic/claude-sonnet-5
jevskill cu run "Open Notepad and type \"hello\"" --dry-run      # plan for real, simulate the steps
jevskill cu stop                                                  # from any other terminal
```

| Flag (`run`) | Default | Effect |
|---|---|---|
| `--model ID` | none | planning model; without it the command is one Jev-only goal |
| `--dry-run` | off | plan, then simulate — the desktop is not touched, no Jev spend |
| `--max-steps N` | `25` | Jev steps per sub-goal |
| `--budget-s S` | `90` | seconds per sub-goal |
| `--usd-cap USD` | `0.50` | stop when Jev + model spend passes this |
| `--ledger-dir DIR` | — | directory holding `.jevskill/ledger.jsonl` (and the stop file) |

**To stop a run:** Ctrl+C in its terminal, **Ctrl+Alt+Esc** on the keyboard, the
mouse in the **top-left corner** of the screen, or `jevskill cu stop` — which
touches `.jevskill/cu.stop`, polled every 50 ms while a run is armed. The check
runs before every decision and again before every action reaches the desktop.
Needs Windows and the `cu` extra (`pip install "jevskill[cu]"`) for a live run;
a dry run works anywhere. The console exposes the same operator as
`GET /api/cu/status`, `GET /api/cu/models`, `POST /api/cu/start|plan|stop|confirm`.

## The zero-install scripts (`skills/jev/scripts/`)

```bash
python scripts/jev.py doctor                      # delegates to the package if importable
python scripts/jev_query.py --state-file log.txt --question-type noul --name breaks_api \
  --instructions 'Does `state` break a public API?' --true-text 'yes' --false-text 'no'
python scripts/jev_query.py --state-file build.log --reduce --keep 8   # gate, keep hits
python scripts/jev_query.py --state-file flags.yaml --blocks --reduce --keep 8
python scripts/jev_recovery.py --list             # read back what REDUCE rejected
python scripts/jev_recovery.py rc_1a2b3c4d5e6f --grep "payment" --all
```

**`--blocks`** gates a *structural unit* rather than a line: a header (`key:`) plus
the scalars nested under it, packed into windows that never split one. Use it when
the judgement compares two lines — "is `prod` different from `default`?" — which no
single line can answer. It accepts a raw text or YAML file, not only a JSON array,
and on flat text it degrades to one block per line, so logs and CSVs behave exactly
as before. Measured: it took the A/B suite's `yaml_drift` row from **0/3 to 3/3**
with no change to the other five workloads.

**REDUCE never drops what it could not judge.** If the gate returns no verdict for
an item, that item is reported in `unjudged`, **kept** (and never cut by `--keep`),
and counted separately — a missing verdict is not the same as a confident "0.0", and
filing it as rejected would be a silent loss. `kept ∪ rejected ∪ unjudged` always
accounts for every input item.

## Flags shared by the data-sending commands

| Flag | Default | Effect |
|---|---|---|
| `--no-redact` | off (redaction **on**) | send state exactly as given |
| `--redact-emails` | off | also scrub email addresses (the address is often the signal) |
| `--redact-extra REGEX` | — | extra regexes to scrub, labelled `extra_0`, `extra_1`, … |
| `--review-below FLOAT` | `0.75` | confidence floor; below it an answer needs review |
| `--review-margin FLOAT` | `0.10` | minimum gap between the top two options for a `choice` |
| `--provider` | auto | `openrouter` or `typesafe`; auto = `JEVSKILL_PROVIDER`, else the first key found by name, vendor names (`JEV_API_KEY`, `TYPESAFE_API_KEY`) first |
| `--json` | off | machine-readable output |

### `ask --cache` — do not pay twice for the same question

*Package CLI only: the zero-install script does not carry a cache, because a cache
is state that belongs beside the ledger.*

| Flag | Default | Effect |
|---|---|---|
| `--cache` | **off** | reuse a response when the request is byte-identical |
| `--cache-ttl SECONDS` | `900` | how long a cached decision stays usable |

Off by default: a stale decision is worse than a paid one when the state is moving.
A hit reports `"cached": true` with its age, spends **zero tokens and zero cost**
(the model did no work), and its ledger row carries `extra: {"cache": "hit"}` so a
zero-cost row is explained rather than mysterious. The key hashes the whole request
body, so a hit means byte-identical input — not merely similar.

### `batch --skip-regex REGEX` — rules before the model

*Package CLI only.* Drop items matching a regex before anything is sent. A
known-noise rule costs nothing: on 8 log lines of which 4 were `DEBUG`, this cut
input tokens **990 → 626 (−36.8%)**. Dropped items are reported as `skipped` /
`skipped_count`, never as items Jev judged — the rule is yours, and presenting it
as a model decision would inflate the saving. A pattern matching everything is an
error, not an empty run.

Redaction is **default-on** because this skill sends your data to a third party by
design. It scrubs credential-shaped strings (private keys, provider and cloud keys,
tokens, `password=`-style parameters) and records what it scrubbed under
`"redactions"` in the output and in the ledger row. It is not a general PII policy:
emails, hostnames and business data pass through unless you ask otherwise.

`--out` writes per-item results for `batch`; `--quiet` skips the tally.