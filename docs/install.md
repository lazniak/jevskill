# Install jevskill — a guide an agent can follow

This file exists to be **read and executed by a coding agent**, not by a human.
It is deliberately explicit about what to check and what to verify at the end, so
the installation either succeeds and is proven, or fails with a specific reason.

Give your agent this prompt:

```text
Install jevskill for my current agent. Read and follow
https://raw.githubusercontent.com/lazniak/jevskill/main/docs/install.md
```

---

## 1. Which agent am I?

Pick the first that applies. Do not install two ways — duplicate copies of a skill
compete for the same triggers.

| Agent | How |
|---|---|
| Claude Code | `npx skills add lazniak/jevskill -g` |
| Codex, Cursor, Gemini CLI, Windsurf, DSH, and 20+ others | `npx skills add lazniak/jevskill -g` |
| None of the above / offline | clone the repo and point the agent at `skills/jev/` |

`-g` installs globally; drop it to install into the current project only.

## 2. Check the environment

```bash
python --version          # must be 3.9 or newer
```

Nothing else is required: the Skill's scripts use only the Python standard library,
so there is nothing to compile and no dependency to install.

**The Skill works with no API key for everything except the actual decisions.**
`plan` and `patterns` are free and offline, and the scripts validate input without
network access. Do not ask for a key merely to check that the install worked.

## 3. Get a key — or find out you already have one

Check **only for the presence** of a key. Never print its value.

```bash
# any one of these is enough
test -n "$OPENROUTER_API_KEY" && echo "openrouter key present"
test -n "$TYPESAFE_API_KEY"   && echo "typesafe key present"
```

If neither is set, tell the user and ask which they prefer:

- **A — real key.** OpenRouter (<https://openrouter.ai/settings/keys>) or TypeSafe
  (<https://console.typesafe.ai/keys>). Then stop and let the user install it; never
  collect a secret in chat.
- **B — no key.** The skill may still be used in *simulation*, but only with the
  user's explicit consent for the current task, and every simulated answer must be
  labelled `mode: agent_simulation`, `jev_called: false`, with `probability` and
  `confidence` set to `null`. See `SKILL.md` §0.

Do not silently choose B. Do not treat an API error as consent to simulate.

## 4. Verify the installation — offline first

```bash
python -m jevskill plan "classify these 3000 support tickets into six categories"
```

Expected: `USE JEV`, the pattern `triage`, and an expected call count. This spends
nothing and needs no key.

Then, **only if a key is present and the user agreed to spend** (a decision costs
about $0.000013):

```bash
python -m jevskill doctor
```

Expected: `jevskill <version> — OK`, the endpoints, and a stage breakdown where
`http` dominates and `build` is near zero. If `build` is large, the timing model has
regressed and the report is wrong — say so rather than trusting it.

With the bundled script only (no package installed):

```bash
python scripts/jev_query.py --help         # the zero-install caller
python scripts/jev_recovery.py --list      # reversible REDUCE: what was rejected
```

## 5. What to tell the user

- where the skill was installed;
- which provider was detected (or that none was found);
- the result of the free verification step, and of `doctor` if it ran;
- that redaction of credential-shaped strings is **on by default**, so the state
  sent is not byte-identical to what was passed in, and `--no-redact` turns it off;
- that exit code `2` means *"an answer needs review"*, not an error.

## 6. If something fails

| Symptom | Cause | Fix |
|---|---|---|
| `npx: command not found` | Node/npm missing | install Node, or clone the repo and use `skills/jev/` directly |
| `no module named jevskill` | package not installed, script run from elsewhere | run `python scripts/jev.py` (it finds the package), or `pip install -e .` |
| `No API key for provider 'openrouter'` | key not in this shell's environment | `setx` does not affect open shells; open a new one, or use `--provider typesafe` |
| exit `2` | at least one answer needs review | not a failure — escalate, widen the state, or re-ask |
| exit `3` | state over the token budget, nothing sent | cut the data or use `--reduce` |
| HTTP `401`/`403` | key wrong or not authorised for this endpoint | check the key's account, and that the provider matches the key shape |
| HTTP `422` | wrong model name for the endpoint | do not pass `jev-latest` to OpenRouter or `typesafe/jev-1.13` to TypeSafe — the client translates, so this means an override is set |

## 7. Optional: the measurement half

The Skill alone decides. The package adds the ledger, `stats`, `advice`, the decision
cache and `batch --skip-regex`/`--dedupe`:

```bash
python -m pip install -e ".[fast]"
```

None of it is required for a single decision, and none of it is needed to use the
Skill from another language — everything is a CLI call that prints JSON.