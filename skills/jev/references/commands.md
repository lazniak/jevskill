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
```

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
| `--provider` | auto | `openrouter` or `typesafe` |
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