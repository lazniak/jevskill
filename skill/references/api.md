# The Decisions API — exact shapes

Everything here is verified against the live API from this repository. Where a
number differs between sources, the discrepancy is called out rather than hidden.

## Endpoint

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

TypeSafe's own first-party endpoint is `POST https://api.typesafe.ai/v1/systemone`
with model routes `jev-latest` / `jev-preview` / `jev-1.13.0`. This skill targets
OpenRouter, because that is a key most developers already have.

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

> **Context discrepancy.** OpenRouter reports 32K; third-party guides and
> TypeSafe's own marketing page say 64K, and the independent guide states the
> hard limit is 64K with a tighter 32K ceiling on "state plus your longest single
> question". This skill budgets **8,000 tokens** by default and treats 32K as the
> ceiling, so the disagreement cannot bite. Raise `--max-state-tokens` only if you
> have measured that accuracy holds for your data.

## Request

```jsonc
{
  "model": "typesafe/jev-1.13",   // required
  "state": { },                   // required — string | object | array
  "questions": { },               // required — name -> question
  "session_id": "...",            // optional, max 256 chars, for observability
  "user": "...",                  // optional, max 256 chars
  "provider": { },                // optional — provider routing preferences
  "trace": { }                    // optional
}
```

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

## Errors

| Code | Meaning | What to do |
|---|---|---|
| 400 | Malformed request (bad question type, empty criteria) | Fix the shape; do not retry |
| 401 | Bad or missing key | Fix the key |
| 402 | No credits | Top up, or fall back to the LLM path |
| 403 | Key lacks access | Check model access |
| 404 | Wrong endpoint | Use `/api/alpha/decisions` |
| 413 | Payload too large | Chunk or prefilter with code |
| 429 | Rate limited | Back off; batch questions instead of looping |
| 502 / 529 | Provider error / overloaded | Retry with backoff |

Error body: `{"error": {"code": <int>, "message": "..."}}`.

The client raises `JevApiError` carrying a `hint` that says which of these
actions to take, and a `retryable` flag. It retries 429/502/529 automatically
with exponential backoff and never retries a 4xx that cannot succeed.

## Getting a key

* **OpenRouter** — <https://openrouter.ai/keys>. Works immediately with existing
  credits; this is what the skill uses.
* **TypeSafe first-party** — waitlisted, keys issued in batches.
* **Gateways** — Vercel AI Gateway (`typesafe-ai/jev`), Cloudflare AI Gateway.
  Their billing, limits and privacy terms apply instead of OpenRouter's.

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
