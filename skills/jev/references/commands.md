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
python scripts/jev_recovery.py --list             # read back what REDUCE rejected
python scripts/jev_recovery.py rc_1a2b3c4d5e6f --grep "payment" --all
```

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

Redaction is **default-on** because this skill sends your data to a third party by
design. It scrubs credential-shaped strings (private keys, provider and cloud keys,
tokens, `password=`-style parameters) and records what it scrubbed under
`"redactions"` in the output and in the ledger row. It is not a general PII policy:
emails, hostnames and business data pass through unless you ask otherwise.

`--out` writes per-item results for `batch`; `--quiet` skips the tally.