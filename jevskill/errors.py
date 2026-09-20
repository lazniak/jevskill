"""Exceptions raised by the JEV client."""

from __future__ import annotations

#: Statuses worth retrying. 429 and the 5xx family are transient by nature; 520
#: is Cloudflare's "unknown error" and was observed in practice while running
#: this repository's own benchmark, which is why it is listed explicitly rather
#: than left to a generic range check.
RETRYABLE_STATUS = frozenset({0, 429, 500, 502, 503, 504, 520, 522, 524, 529})


class JevError(Exception):
    """Base class for every JEV failure."""


class JevConfigError(JevError):
    """No API key, or an unusable configuration."""


class JevApiError(JevError):
    """The Decisions API returned a non-200 status.

    ``hint`` translates the documented status codes into the action a harness
    should actually take, so the model reading the error knows whether to retry,
    shrink the payload, or stop entirely.
    """

    HINTS = {
        400: "Malformed request — check question types are exactly noul/choice/score.",
        401: "Bad or missing API key. Set OPENROUTER_API_KEY (or TYPESAFE_API_KEY for the vendor endpoint).",
        402: "Out of OpenRouter credits. Top up at openrouter.ai/credits, or fall back to the plain LLM path. (The vendor endpoint does not use this code.)",
        403: "Key lacks access to this model.",
        404: "Wrong endpoint for the provider. OpenRouter uses /api/alpha/decisions; TypeSafe uses /v1/systemone. Check --provider.",
        413: "State too large for the context window. Chunk it, or prefilter with code.",
        422: "The request body failed validation (TypeSafe's equivalent of a 400) — a missing required field or a malformed question. The body names the offending field; fix it rather than retrying.",
        429: "Rate limited. Back off and retry; batch your questions instead of looping.",
        502: "Provider error. Retry — usually transient.",
        503: "Provider unavailable. Retry — usually transient.",
        504: "Provider gateway timeout. Retry, possibly with a smaller state.",
        520: "Provider returned an unknown error (Cloudflare-style 520). Retry — observed transient in practice.",
        529: "Provider overloaded (both OpenRouter and TypeSafe use this). Retry with backoff, or fall back to the LLM path.",
    }

    def __init__(self, status: int, message: str, body: str = "") -> None:
        self.status = status
        self.message = message
        self.body = body
        self.hint = self.HINTS.get(status, "Unexpected status.")
        super().__init__(f"HTTP {status}: {message} — {self.hint}")

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUS


class JevQuestionError(JevError):
    """A question was built with unusable criteria."""