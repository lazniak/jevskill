# Working in this repository

Guidance for an AI agent (or a person) changing `jevskill`. Read this before
editing; the conventions here are load-bearing, not decoration.

## What this project is

A portable **Agent Skill** that lets a coding harness use the Jev decision model
(TypeSafe System One, via OpenRouter or TypeSafe's own API) for bounded decisions
— routing, triage, gating, grading, ranking, reducing large data — plus an
effectiveness ledger so the skill's own value is measured rather than asserted.

Two halves, deliberately separate:

| Path | What it is |
|---|---|
| `skills/jev/` | the Skill: `SKILL.md`, `references/`, and stdlib-only `scripts/` |
| `jevskill/` | the Python package: client, primitives, orchestration, ledger, CLI |
| `jevskill/cu/` | the computer-use half: `observe` (Windows UIA through `comtypes`, the `cu` extra), `reduce`, `hashing` — everything but `snapshot()` is pure Python and runs on Linux CI |
| `jevskill/web/` | the local console: a stdlib `ThreadingHTTPServer` and the three static files it serves (`web/static/`, shipped as package data) |

The Skill must work with **no install** (stdlib only). The package adds the
measurement half (ledger, `stats`, `outcome`, `plan`). Keep that boundary: if you
add a feature, decide which half it belongs to and do not make the Skill depend on
the package. A provider quirk belongs in **both**: `PROVIDERS` in
`jevskill/config.py`, and its copy in `skills/jev/scripts/jev_query.py`.

## Two providers

`openrouter` and `typesafe` serve the same model, at the same price, and differ in
more than a URL. Every delta lives in `PROVIDERS` in `jevskill/config.py`; do not
scatter provider conditionals through the client.

The one that bites: **TypeSafe returns no `cost` field.** Recording that as zero
would make every vendor decision look free and inflate the reported savings. The
client computes it from the documented rate and sets
`usage.cost_source = "computed" | "provider"`. Any new provider needs the same
treatment.

Model names are translated per provider (`typesafe/jev-1.13` ↔ `jev-latest`)
because passing the wrong one is a 404 or a 422.

**Provider intent is resolved before the key is looked up.** `Config.from_env`
asks "what did the user state?" (`--provider`, `JEVSKILL_PROVIDER`, config file)
and only then searches for a key — scoped to that provider when one was stated,
otherwise **by name with the vendor's names first** (`JEV_API_KEY`,
`TYPESAFE_API_KEY`, then the OpenRouter names), each in the environment and then
the registry. Measured 2026-09-20: the reverse order sent an OpenRouter key to the
vendor (401) and kept using the aggregator after a vendor key had been added. The
zero-install script carries the same rule (`find_api_key` in `jev_query.py`) and
`tests/test_key_precedence.py` pins both.

**The vendor rejects unknown top-level fields.** `session_id` (an OpenRouter
extension) is a `400 api_usage_error` on `api.typesafe.ai`; `PROVIDERS[...]
["accepts_session_id"]` gates it in the client. A new body field goes through the
same gate.

**Never print key material.** `doctor` reports `key_name`, `key_source`
(`env`/`registry`/`file`) and an 8-hex SHA-256 `key_fingerprint` — never a prefix
of the key, which an earlier version did.

### Adding a third provider

The model is also served by Cloudflare Workers AI (`typesafe/jev`), Vercel AI
Gateway (`typesafe-ai/jev`) and two resellers. Adding one is a documented task, and
it was deliberately **not** done speculatively — see 0.9.0 in the CHANGELOG. To add
one honestly:

1. **Make a real call first**, with a real account. The exact body shape, whether the
   endpoint wraps the response, whether `cost` is reported, what the model field is
   called — all of it is only knowable from a live response. Both existing providers
   have now been exercised live (OpenRouter from the first release, the vendor on
   2026-09-20 — which is how the `session_id` rejection and the key-precedence bug
   were found); do not add a third from documentation alone.
2. Add the entry to `PROVIDERS` in `jevskill/config.py` **and** the compiled copy in
   `skills/jev/scripts/jev_query.py` (the Skill must run with nothing installed).
3. If the endpoint does not report `cost`, set `reports_cost: False` — the client
   computes it from `INPUT_PRICE_PER_MTOK` and marks `usage.cost_source`. Recording
   a missing cost as zero makes every decision look free and inflates every saving
   figure in the README.
4. If the response is wrapped (`{"result": {...}}`) or the request omits `model`,
   that is a client change, not a config entry. Keep the branch in the client's
   normalisation rather than scattered through the CLI.
5. Add the provider to `tests/test_providers.py`, and a row to the provider tables in
   `README.md` and `references/api.md`.
6. Only then claim support in the README. An unverified provider is a claim.

## Running things

```bash
python -m pytest -q                       # 1367 tests, offline, must stay green
python bench/run.py --legacy-reduce       # live API: E1-E7, writes bench/results.json
python bench/ab.py --runs 3               # live API: the A/B evaluation, writes bench/ab_results.json

python skills/jev/scripts/jev_query.py --help       # the bundled, zero-install caller
python skills/jev/scripts/jev_recovery.py --list    # read back what REDUCE rejected
python -m jevskill patterns                         # the usage palette
python -m jevskill advice                           # KEEP / STOP / ESCALATE per pattern
python bench/batch_bench.py                         # live API: batch vs per-item
```

The benchmark scripts spend real money (a few cents) and need a key —
`JEV_API_KEY` (vendor) or `OPENROUTER_API_KEY`; set `JEVSKILL_PROVIDER` to pick the
route explicitly. Tests never touch the network.

## Conventions that matter

**Every published number must be reproducible.** If a figure appears in `README.md`
or `skills/jev/references/`, some script in this repo must produce it. Never round
a number up, never quote a figure you cannot regenerate, and never replace a
measured value with an estimate.

**Negative results stay published.** `bench/run.py --legacy-reduce` deliberately
reproduces a design that failed (99% context reduction while keeping 1 of 8 wanted
lines). The `shortlist` narrowing path did not trigger in testing and the README
says so. If you find a case where Jev loses, add it rather than removing the test.

**Name the target value in every question.** The single most expensive mistake in
this codebase's history. A question saying "does this line report an error?" while
20 lines sit in one state returns a flat, meaningless ~0.74 for every line. Point
at the value with a backticked path (`` `L7` ``) in *both* instructions and
criteria. This is measured and documented in `references/prompting.md` §1 — do not
"simplify" it away.

**Fan out, do not loop.** Questions sharing one state run in parallel: 8 in one
call was 12.4× faster and used 4.03× fewer tokens than 8 sequential calls. Any
loop that calls `decide()` once per question is a bug.

**One chars-per-token constant.** `CHARS_PER_TOKEN` in `jevskill/config.py` is
calibrated against the live API (a prose rule of thumb under-counted logs by
2.15×). Route all sizing through `count_tokens()`; do not open-code a division.

**Ledger writes are append-only.** Outcomes are stored as separate patch records
keyed by `decision_id`, never by rewriting a decision line.

**The web console is local, stdlib and shares the CLI's writer.** `jevskill/web/`
binds `127.0.0.1` only — `make_server` raises `ValueError` on any other address and
the CLI offers no `--host` — because the endpoint is unauthenticated and spends a
live key; a `Host` check and an `Origin` check close DNS rebinding and CSRF, which
loopback alone does not. It imports nothing outside the standard library, and its
three static files under `web/static/` are **package data** (`pyproject.toml`), not
documentation: drop that entry and `jevskill web` 404s from a wheel while working
from a checkout. Content types are stated, never guessed — `mimetypes` reads the
Windows registry, where `.js` is routinely `text/plain`. Above all it does not
re-implement `ask`: redaction, the review thresholds, `count_tokens` and the ledger
writer are the CLI's own functions, so a console decision is one row of the same
shape, tagged `which="web"`. Adding a field to one row format and not the other is
how a ledger starts describing two different things.

**Explicit paths mean exactly those paths.** `load_records([p])` must not merge
the global ledger; that silently contaminated a per-project report once.

## Style

- Python 3.9+, standard library by default. `httpx`/`orjson` are optional speedups.
- Docstrings explain **why**, including the mistake that motivated the code. This
  codebase's comments are its memory; a comment restating the code is noise.
- Tests derive from constants rather than hard-coding them, so re-tuning a
  constant does not require editing assertions.
- `SKILL.md` is a decision guide for an agent, not documentation for a human. Keep
  it imperative and under 250 lines: every paragraph must change what an agent does
  next. Measurement prose, ledger reports and provider history belong in
  `references/` — move detail there instead of adding to it, and add the file to
  the §7 router when you do (a test fails if a reference is left unrouted).
- Commit messages: conventional commits, and explain the reasoning and the
  measurement, not just the change.

## Before you push

1. `python -m pytest -q` is green.
2. If you changed a measured behaviour, re-run the relevant benchmark and update
   `README.md`, `CHANGELOG.md` and the reference doc together.
3. If you changed a published number, say so explicitly in the CHANGELOG and name
   the superseded figure.
4. Bump the version in `pyproject.toml` and `jevskill/__init__.py` together.
