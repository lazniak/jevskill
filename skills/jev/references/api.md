# The Decisions API — exact shapes

Every claim here is either **quoted from the vendor's docs** (with the URL, in
[Sources](#sources-every-row-above-with-its-url)) or **measured from this
repository against the live API** (with the date). Where a number differs between
sources, the discrepancy is called out rather than hidden.

Provenance in one line: the OpenRouter route has been called from here since
v0.1.0; the **vendor endpoint's first real decision was 2026-09-20** — before that
date only its `401` shape had been observed, and this file used to imply more.

> **Start here when you need something this file does not cover.** TypeSafe
> publishes a machine-readable index of every documentation page at
> **<https://docs.typesafe.ai/llms.txt>** — one file, one line per page, each with a
> `.md` URL you can fetch directly. It is the only reliable way to reach the
> cookbooks, the primitives and the
> [jaggedness page](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md). Fetch it
> before guessing a URL.

## Two providers, one model

The same model is served through two endpoints, and this skill supports both.
They differ in more than a URL, so a naive swap fails in ways that are easy to
miss — most importantly, one reports a billed cost and the other does not.

| | **OpenRouter** | **TypeSafe (vendor)** |
|---|---|---|
| Endpoint | `POST https://openrouter.ai/api/alpha/decisions` | `POST https://api.typesafe.ai/v1/systemone` |
| Model field | `typesafe/jev-1.13` | `jev-latest` (→ `jev-1.13.0`) |
| Aliases | — | `jev-latest`, `jev-preview` |
| Key variable | `OPENROUTER_API_KEY` etc. | `JEV_API_KEY`, `TYPESAFE_API_KEY` |
| Key shape | `sk-or-v1-…` | vendor-issued (`apikey_…`, 108 chars observed) |
| `session_id` in the body | accepted | **HTTP 400 `api_usage_error`** — the client omits it |
| Measured live (2026-09-20, Poland, warm p50) | 325–371 ms | **292–320 ms** |
| Context | 32,000 tokens | **64,000** per request; 32,000 for state + longest question |
| Price | $0.042 / Mtok input, output free | **identical** — $0.042 / Mtok, output free |
| Rate limits | not documented here | 250,000 tok/s, 1,200 req/min, **dynamic** |
| `usage.cost` | ✅ reported | ❌ **absent** — computed from the rate |
| Response `id` | ✅ | ❌ absent |
| Response `provider` | ✅ | ❌ absent |
| Choice options | (undocumented) | documented max **255** |
| Score levels | (undocumented) | documented min 2, max **10** |
| Error codes | 400, 401, 402, 403, 404, 413, 429, 502, 529 | **400** (`api_usage_error`, unknown field), **422** (validation), 401, 429, **529** |
| Model listing | `GET /api/v1/models` | `GET /v1/models` |
| Access | immediate with credits | waitlist, keys in batches |

### Sources: every row above, with its URL

Each row is either quoted from a page (fetched and verified 2026-09-20, all HTTP
200) or measured from this repository on the date given. Nothing in the table is
inferred from a third-party summary.

| Row | Source | Quoted |
|---|---|---|
| Endpoint (vendor) | [api.md](https://docs.typesafe.ai/api.md) | "POST https://api.typesafe.ai/v1/systemone" |
| Endpoint (OpenRouter) | measured here, every release; the 400 body on `/chat/completions` is quoted below | — |
| Model field, aliases | [models.md](https://docs.typesafe.ai/models.md) | `jev-latest` → `jev-1.13.0`, "The most recent stable, official release" |
| `jev-preview` | [models.md](https://docs.typesafe.ai/models.md) | "There is no preview build available right now." |
| Key variable, key shape | measured here 2026-09-20 (`jevskill doctor`, a real vendor key) | — |
| `session_id` → 400 | measured here 2026-09-20, first live vendor call | `{"detail":{"error_type":"api_usage_error"}}` |
| Measured live p50 | measured here 2026-09-20, `bench/cu_bench.py`, Poland, warm | — |
| Context (vendor) | [models.md](https://docs.typesafe.ai/models.md) | "64k tokens per request; 32k tokens for `state` plus the longest question" |
| Context (OpenRouter) | [openrouter.ai/typesafe](https://openrouter.ai/typesafe) | "32K context" |
| Price | [models.md](https://docs.typesafe.ai/models.md) · [openrouter.ai/typesafe](https://openrouter.ai/typesafe) | "\$42 / \$0.042" per Btok/Mtok, "Charged per input token. Output tokens are free."; "$0.042 /M input tokens" |
| Rate limits | [models.md](https://docs.typesafe.ai/models.md) | "250,000 tokens per second / 1,200 requests per minute"; "Rate limits are adjusting dynamically" |
| `usage.cost`, response `id`, response `provider` | measured here 2026-09-20 — the vendor response carried `usage: {input_tokens, output_tokens}` and nothing else | — |
| Choice options | [primitives/choice.md](https://docs.typesafe.ai/primitives/choice.md) | "A Choice question accepts up to 255 options" |
| Score levels | [primitives/score.md](https://docs.typesafe.ai/primitives/score.md) | "Should have at least two levels; the API accepts up to 10." |
| Error codes (vendor) | [api.md](https://docs.typesafe.ai/api.md) | 401 "Missing or invalid API key", 422 "failed validation", 429, 529 "TypeSafe is temporarily overloaded" |
| 400 `api_usage_error` | measured here 2026-09-20 — not in the vendor's error table | — |
| Model listing | [models.md](https://docs.typesafe.ai/models.md) | "`GET /v1/models` returns the names your account can send in the `model` field" |
| Access | vendor console link as given by [agent-skill.md](https://docs.typesafe.ai/agent-skill.md) ("create an API key" → `console.typesafe.ai/keys`, not fetched here — it needs a login) · <https://openrouter.ai/keys> | — |
| Request/answer shapes | [api.md](https://docs.typesafe.ai/api.md) | "Evaluate a `state` against a map of typed `questions` and get back structured `answers`" |
| OpenRouter alias | [openrouter.ai/typesafe](https://openrouter.ai/typesafe) | "set the model to an ID such as `~typesafe/jev-latest`" |

The alias `~typesafe/jev-latest` is OpenRouter's own tilde form, listed 2026-09-18;
this skill sends the explicit `typesafe/jev-1.13` so a moving alias cannot silently
change a tuned threshold.

```bash
jevskill doctor --provider typesafe     # probe the vendor endpoint
jevskill doctor --provider openrouter   # probe OpenRouter
jevskill doctor                         # auto-detect from the key
```

The skill picks a provider in this order: an explicit `--provider`, then
`JEVSKILL_PROVIDER`, then a `"provider"` field in `~/.jevskill/config.json`. A
stated provider is resolved **before** the key is looked up and only its own key
variables are consulted — measured 2026-09-20, the reverse order sent an OpenRouter
key to the vendor and got a 401. When nothing is stated, keys are searched **by
name**, vendor names first (`JEVSKILL_API_KEY`, `JEV_API_KEY`, `TYPESAFE_API_KEY`,
then `OPENROUTER_API_KEY`, `OPEN_ROUTER_API_KEY`, `JEVUSE_API_KEY`), each in the
environment and then in `HKCU\Environment`; the shape of the key found decides
(`sk-or-…` is OpenRouter, anything else the vendor), else OpenRouter.

**Model names are translated automatically** in both directions, because passing
`typesafe/jev-1.13` to the vendor endpoint or `jev-latest` to OpenRouter is a 404
or a 422 and is the easiest mistake to make when switching.

### Choosing between them

* **OpenRouter** — a key you may already have, immediate access, and the provider
  reports the actual billed `cost`, so the ledger needs no arithmetic.
* **TypeSafe** — the vendor's own endpoint, double the context (64K), documented
  rate limits and option ceilings. It is the **same price**, so going direct is a
  dependency and features question, not a cost one.

## Endpoint (OpenRouter)

```
POST https://openrouter.ai/api/alpha/decisions
Authorization: Bearer $OPENROUTER_API_KEY
Content-Type: application/json
```

**This is not `/api/v1/chat/completions`.** Jev has no `messages` array and
returns no `choices[0].message.content`. Pointing an OpenAI-compatible SDK at it
fails outright:

```json
{"error": {"message": "typesafe/jev-1.13 is a decisions model and cannot be used
with the chat/completions endpoint. Use the /api/alpha/decisions endpoint instead.",
"code": 400}}
```

## Endpoint (TypeSafe, first-party)

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer $TYPESAFE_API_KEY
Content-Type: application/json
```

The vendor's own API is the reference implementation, not a compatibility shim:
TypeSafe documents the same `state` + `questions` shape, so the request body this
skill builds is **identical for both providers** — only the URL, the `model` name
and the optional `session_id` differ. First real decision against the vendor:
**2026-09-20** — `model: "jev-1.13.0"`, `usage: {"input_tokens": 311,
"output_tokens": 21}`, no `id`, no `cost`, warm p50 292–320 ms from Poland (the
OpenRouter route measured 325–371 ms in the same minutes). Both `/v1/systemone` and
`/v1/models` return a structured `401` for an invalid key:

```json
{"detail": {"error_type": "authentication_error",
            "message": "Cannot authenticate with the server. Please check your API key and try again."}}
```

Its documented limits are more generous than OpenRouter's page suggests, and four
of them are worth knowing (sources in the table above):

* **Choice: up to 255 options** per question. This skill warns past 40 anyway — not
  because the API refuses more, but because accuracy on adjacent options degrades
  well before the ceiling.
* **Score: 2 to 10 levels.** This skill caps at 7, again for calibration rather
  than for the API.
* **Rate limits are explicitly dynamic.** TypeSafe says the published numbers can
  change without notice while they scale. Treat any limit as a hint and honour
  `retry-after` when it is present.
* **Aliases move.** `jev-latest` tracks the newest stable release, so answers can
  change without a change on your side. The response's `model` field reports the
  versioned id that actually answered — log it. If you have tuned a confidence
  threshold against a version, pin that version's id instead of the alias.

### The missing cost field

TypeSafe returns `usage: {input_tokens, output_tokens}` and **no `cost`** — the one
field that would silently corrupt a ledger. Recording it as `0` would make every
vendor-endpoint decision look free and inflate the reported savings, which is
exactly the class of error this project exists to prevent. So the client computes
it from the documented rate and marks the provenance:

```jsonc
"usage": {"input_tokens": 1000, "output_tokens": 20,
          "cost": 0.000042, "cost_source": "computed"}
```

`cost_source` is `"provider"` when the endpoint reported the number itself.

## Model facts (OpenRouter)

| Field | Value |
|---|---|
| Model id | `typesafe/jev-1.13` |
| Snapshot id | `typesafe/jev-1.13-20260917` |
| Modality | `text` → `decisions` |
| Context | **32,000 tokens** |
| Price | **$0.042 / 1M input**, **$0.00 / 1M output** |
| Reasoning | no |
| Tool calls | no |
| Streaming | no |

> **Context discrepancy, resolved.** OpenRouter reports 32K. TypeSafe's own docs
> say **64k per request**, of which **32k applies to `state` plus the longest
> single question**. Third-party guides repeating "64K" were quoting the vendor
> figure; both are right about their own endpoint. This skill budgets 8,000 tokens
> by default and treats the provider's limit as the ceiling, so the difference
> cannot bite either way. Raise `--max-state-tokens` only if you have measured
> that accuracy holds for your data.

## Request

```jsonc
{
  "model": "typesafe/jev-1.13",   // required
  "state": { },                   // required — string | object | array
  "questions": { },               // required — name -> question
  "session_id": "...",            // OpenRouter only — optional, max 256 chars
  "user": "...",                  // OpenRouter only — optional, max 256 chars
  "provider": { },                // OpenRouter only — routing preferences
  "trace": { }                    // OpenRouter only
}
```

The last four fields are OpenRouter extensions. **The vendor schema is exactly
`model`, `state`, `questions`**; any other top-level field is a `400
api_usage_error` (measured with `session_id`). The client sends `session_id` only
where the provider accepts it and keeps it on the result for the local ledger.

`state` and `questions` accept nested JSON. One request carries one state and as
many questions as you like, all evaluated **independently and in parallel**.

## Question types

### `noul` — the yes/no gate

```jsonc
{
  "type": "noul",
  "instructions": "Is the customer reporting a software defect?",
  "criteria": {
    "true":  "The customer describes broken or unexpected product behavior.",
    "false": "The customer is asking a question or requesting a feature."
  }
}
```

Returns **only** `noul`: a float in `[0, 1]` = P(true). No confidence, no
distribution — the single probability *is* the distribution. The threshold is
applied by your code.

### `choice` — pick one of N

```jsonc
{
  "type": "choice",
  "instructions": "Which team should own this ticket?",
  "criteria": {
    "payments": "Checkout, billing, or payment processing issues.",
    "frontend": "Rendering, layout, or browser compatibility issues.",
    "account":  "Login, permissions, or profile issues."
  }
}
```

Returns `choice` (the winning key), `confidence`, and `probabilities` — a
probability for **every** option key you sent, including zeros.

### `score` — grade on an ordered rubric

```jsonc
{
  "type": "score",
  "instructions": "How urgent is this ticket?",
  "criteria": [
    "Can wait for the next release",
    "Should be fixed this week",
    "Blocking revenue right now"
  ]
}
```

Array **order defines the scale** (index 0, 1, 2, …). Returns `score` as a
probability-weighted mean of the indices — so `1.99` is a normal answer, not just
integers — plus `confidence`, `probabilities` keyed by index, and a `legend`
mapping indices back to your labels.

### Structured instructions and criteria

`instructions` and every criteria value may be an object or array instead of a
string. TypeSafe recommends this when:

* the question needs examples or background (put them in named fields),
* part of the question comes from your code (its own field, not string-spliced),
* several questions share wording (keep field names identical so the model can
  compare options directly).

A contrastive Choice, which is the single biggest accuracy lever available:

```jsonc
{
  "type": "choice",
  "instructions": {"question": "Which subsystem owns `ticket.message`?",
                   "focus": "Classify the primary request."},
  "criteria": {
    "billing": {
      "what": "Charges, invoices, refunds, subscriptions",
      "not_for": "Order tracking or account access",
      "examples": ["I was charged twice", "Where is my refund?"]
    },
    "orders": {
      "what": "Order status, delivery, cancellation, returns",
      "not_for": "Charges or account access",
      "examples": ["Where is my order?", "Cancel my shipment"]
    }
  }
}
```

Use a backticked dot-and-index path (`` `ticket.message` ``) to point a question
at a specific value inside the state.

## Response

```jsonc
{
  "id": "gen-dec-1789738314-X5e5eKGQdvR9rblyX250",
  "model": "typesafe/jev-1.13-20260917",
  "provider": "TypeSafe",
  "answers": {
    "is_bug": {"type": "noul", "noul": 0.96},
    "team": {
      "type": "choice",
      "choice": "payments",
      "confidence": 0.75,
      "probabilities": {"payments": 0.84, "frontend": 0.16, "account": 0}
    },
    "urgency": {
      "type": "score",
      "score": 1.99,
      "confidence": 0.99,
      "legend": {"0": "Can wait for the next release",
                 "1": "Should be fixed this week",
                 "2": "Blocking revenue right now"},
      "probabilities": {"0": 0, "1": 0.01, "2": 0.99}
    }
  },
  "usage": {"cost": 0.000019992, "input_tokens": 476, "output_tokens": 70}
}
```

Answers come back under the **same keys you chose**, which is what makes fan-out
ergonomic. `model` in the response is the resolved snapshot id, not your alias.

### Reading the numbers correctly

* **`noul`** is a probability, not a boolean. `0.62` means "leaning yes". Choosing
  to treat that as `true` is a policy decision your code makes.
* **`confidence`** is the model's self-reported certainty. It is *calibrated* —
  optimized against outcomes — but calibration is measured across groups of
  predictions, so it does not guarantee any individual answer is correct.
* **`probabilities`** include every option you supplied. Zeros are common and are
  informative: they mean the option was considered and ruled out.
* **`score`** is a weighted mean, so it lands between levels. Do not round it away
  before you have used it — `1.4` vs `1.6` is signal.

Three sharper points, from the vendor's own guidance, that change how you should
read these fields:

* **`confidence` measures distribution concentration, not correctness.** It
  summarizes how peaked the probabilities are. It is *not* a statement about
  whether the workflow is right, and it is not permission to act. Several
  genuinely acceptable alternatives also spread probability, so low confidence on
  a harmless preference choice is expected and does not invalidate it.
* **`noul` near 0.5 means "as likely yes as no", not "medium intensity".** Do not
  read a 0.5 gate as a middling severity score — it is a coin flip, and the right
  response is to escalate or supply more state.
* **A Choice cannot select a value you did not offer.** If you build candidates in
  code and let the model pick one, check *candidate coverage* first: an omitted
  value is unreachable, and the model will confidently pick the nearest option it
  was given. This is the same failure mode as a missing `unclear` option, one
  level up.

### Also worth reading

TypeSafe publishes its own agent skill, and it is **complementary to this one**
rather than a competitor. Per
[agent-skill.md](https://docs.typesafe.ai/agent-skill.md), installation is:

```bash
# Claude Code — "Run these two commands in your terminal"
claude plugin marketplace add typesafe-ai/skills
claude plugin install typesafe@typesafe-ai

# any other agent
npx skills add typesafe-ai/skills --skill typesafe-ai
```

"Choose one installation method to avoid duplicate copies." Updates:
`claude plugin marketplace update typesafe-ai` then `claude plugin update
typesafe@typesafe-ai`.

*Theirs* teaches an agent how to **build applications with** Jev — it routes to the
live docs and cookbooks and covers architecture patterns (reranking, hierarchical
classification, extraction cascades, function calling).
*This one* is operational: it **runs Jev during a session**, reduces the harness's
context with a reversible REDUCE pipeline, and records measured statistics.

Install both. Theirs if you are writing an app that calls Jev; this one if you want
your coding agent to reach for Jev while working. One piece of their advice applies
to both: "Put the constants (questions and thresholds) in a single place so they're
easy to review."

## Official SDKs — and why this skill ships its own client

TypeSafe publishes first-party SDKs. Use them in an application; this skill does
not, and the reason is structural rather than a judgement about their quality.

**Python** — [sdk/python/usage.md](https://docs.typesafe.ai/sdk/python/usage.md):

```bash
pip install typesafe_sdk
```

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient   # AsyncTypeSafeClient for async

client = TypeSafeClient()
result = client.system_one(state, questions)     # -> result.nouls / .choices / .scores
```

Both `TypeSafeClient` and `AsyncTypeSafeClient` expose `.system_one(state,
questions)`, and the docs show a response model being passed "to make using the
response more *type-safe*".

**JavaScript / TypeScript** — [sdk/javascript.md](https://docs.typesafe.ai/sdk/javascript.md)
(Node 20+):

```bash
npm i @typesafe-ai/sdk
```

```ts
import { choice, TypeSafeClient } from "@typesafe-ai/sdk";   // also noul(), score()

const response = await client.systemOne({ state, questions });
```

"Answer types are inferred from your questions."

**Why this skill reimplements the client instead.** An Agent Skill is a folder an
agent reads and runs *in someone else's repository*. `skills/jev/scripts/jev_query.py`
therefore uses the standard library only: it must work when the target project has
no virtualenv, a conflicting one, or a lockfile you are not allowed to touch —
`pip install` inside a user's project mid-session is not a side effect a skill gets
to have. It also has to speak **both** providers behind one flag, and to compute the
missing `cost` field the vendor does not return, which no SDK does for you.

**Use the SDK instead when** you are writing an application rather than driving a
session: you want typed responses, `async`, connection pooling and the vendor's own
retry policy, and you control the dependency list. The wire format is identical, so
questions written for one work unchanged in the other.

## Errors

| Code | Provider | Meaning | What to do |
|---|---|---|---|
| 400 | OpenRouter | Malformed request (bad question type, empty criteria) | Fix the shape; do not retry |
| **400** | **TypeSafe** | `api_usage_error` — a top-level field the vendor does not define (`session_id`, `user`, `provider`, `trace`) | Remove the field; do not retry |
| **422** | **TypeSafe** | **Validation failed** — missing field or malformed question; body names the field | **Fix it; do not retry** |
| 401 | both | Bad or missing key | Fix the key for *that* provider |
| 402 | OpenRouter | No credits | Top up, or fall back to the LLM path |
| 403 | OpenRouter | Key lacks access | Check model access |
| 404 | both | Wrong endpoint **for the provider** | Check `--provider`: OpenRouter `/api/alpha/decisions`, TypeSafe `/v1/systemone` |
| 413 | OpenRouter | Payload too large | Chunk or prefilter with code |
| 429 | both | Rate limited | Back off; batch questions instead of looping |
| 502 / 503 / 504 / 520 | transient | Provider-side failure | Retry with backoff |
| 529 | both | Provider overloaded | Retry with backoff, or fall back to the LLM path |

Error bodies differ in shape, and both are surfaced as-is:

```jsonc
// OpenRouter
{"error": {"code": 400, "message": "..."}}

// TypeSafe
{"detail": {"error_type": "authentication_error", "message": "..."}}
```

The client raises `JevApiError` carrying a `hint` that says which action to take
and a `retryable` flag. Payload-size errors additionally name the ceiling **for
the provider in use** — 32,000 on OpenRouter, 64,000 (and 32,000 for state plus
the longest question) on TypeSafe — because that number is the one thing a caller
cannot look up from inside an error handler.

Error body: `{"error": {"code": <int>, "message": "..."}}`.

The client raises `JevApiError` carrying a `hint` that says which of these
actions to take, and a `retryable` flag. It retries 429/502/529 automatically
with exponential backoff and never retries a 4xx that cannot succeed.

## Getting a key

* **OpenRouter** — <https://openrouter.ai/keys>. Works immediately with existing
  credits; this is what the skill uses. Model id `typesafe/jev-1.13`, alias
  `~typesafe/jev-latest`.
* **TypeSafe first-party** — <https://console.typesafe.ai/keys>; waitlisted, keys
  issued in batches. A key issued this way was used for the 2026-09-20 measurements
  above.
* **Gateways.** Their billing, limits and privacy terms apply instead of
  OpenRouter's, and **this skill has not been run against either of them** — adding
  a provider here requires a live call first (`AGENTS.md`, "Adding a third
  provider").

| Gateway | Model id | Status |
|---|---|---|
| [Vercel AI Gateway](https://vercel.com/ai-gateway/models/jev) | `typesafe-ai/jev` | **Verified 2026-09-20** (HTTP 200): listed at 32K context, price shown as *Free*, with "Promotional pricing ends on September 25, 2026". It is called through the AI SDK's `experimental_evaluate`, not the chat API. Treat the free window as expiring. |
| Cloudflare Workers AI | `typesafe/jev` | **Reported, unverified.** `developers.cloudflare.com/workers-ai/models/jev/` returned **404** on 2026-09-20 and the Workers AI model catalogue page contained no match for "jev" or "typesafe" — which, per the next subsection, is not proof of absence. Do not put this id in code until a live call proves it. |

### The key will not appear in a model picker

Jev's modality is `text->decisions` with empty `supported_parameters`, so gateway
catalogues built for chat models filter it out. Searching a gateway for "jev" can
return nothing while the model is live and working. **Address it by exact id.**

## Cost arithmetic

Cost depends only on input tokens: state + all question text. Output is free
because there is no output to bill.

```
cost = (state_tokens + question_tokens) / 1,000,000 * $0.042
```

Measured examples from `bench/results.json`:

| Call | Input tokens | Cost |
|---|---|---|
| 1 gate, small state | 317 | $0.0000133 |
| 1 gate, 7K-token state | 7,020 | $0.0002948 |
| 8 questions, one state | 663 | $0.0000279 |
| 8 questions, 8 calls | 2,672 | $0.0001122 |

Two consequences worth internalising:

1. **Batching is a token optimisation, not only a latency one.** Sequential calls
   re-send the same state every time: measured **4.03× token amplification** when
   the same state was sent 8 times.
2. **Cost is not where Jev wins against a cheap chat model.** A small chat model
   answered the same question for $0.0000057 — *less* than Jev's $0.0000133. Jev's
   advantage is structured output with real distributions (no parsing, no retries,
   no malformed JSON), and the token savings that come from not shipping a large
   corpus into an LLM's context at all. Claiming a cost win against a
   flash-lite-class model would not survive scrutiny.
