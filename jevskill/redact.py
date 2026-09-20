"""Redact credential-shaped strings from state before it leaves the machine.

The skill's job is to send the user's data — logs, diffs, tickets — to a third
party. A decision model rarely needs live credentials to classify, so the default
is to scrub the patterns that should never leave a machine and record *what* was
scrubbed in the output, so a redacted run is interpretable rather than silently
different from what the caller passed in.

This is not a complete PII policy: it catches credential-shaped strings, not
personal data. Email addresses are opt-in (`redact_emails=True`) because for many
triage tasks the address *is* the signal. `--no-redact` exists for the rare run
where the secret is the subject under test.

**Cost on a hot path, measured rather than assumed.** A per-step agent loop runs
this on every step, so it was timed before being kept: on a 60-element UI tree
(6,727 characters — the same state the API bills at 6,041 input tokens with the
computer-use question bundle), `redact_state` takes a median of **0.642 ms** over
20 repetitions, and 0.699 ms with `redact_emails=True`. Reproduce with
`python bench/cu_bench.py --micro`; the number is recorded in
`bench/cu_results.json`.

That is well under the 2 ms at which a hash-keyed LRU cache would have been worth
its own invalidation bugs, so there is **no cache**: a step that spends ~300 ms in
the network will not notice 0.6 ms of regex. The cost is linear in the number of
patterns and in the size of the state, so re-measure if you add either.

The bundled zero-install script `skills/jev/scripts/jev_query.py` carries a
compiled copy of these patterns (same rule as `PROVIDERS`): the Skill must work
with nothing installed, so it cannot import this package. Keep the two lists in
step; the test in `test_redact.py` asserts the script's list stays a superset of
the behaviour documented here.
"""

from __future__ import annotations

import re

#: `(label, compiled pattern)`, ordered most-specific first: the first pattern
#: that matches at a position wins, so `sk-or-v1-…` must be tested before the
#: generic `sk-…` or it would be labelled as the wrong kind of secret.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("openrouter_key", re.compile(r"\bsk-or-v1-[0-9a-f]{16,}\b")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}", re.IGNORECASE)),
    ("password_param", re.compile(
        r"\b(password|passwd|api[_-]?key|secret|auth[_-]?token|access[_-]?token|token)"
        r"\s*[=:]\s*[\"']?[A-Za-z0-9._~+/-]{6,}",
        re.IGNORECASE)),
)
EMAIL_PATTERN = ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]{2,}\b"))


def redact_text(
    text: str,
    *,
    redact_emails: bool = False,
    extra_patterns: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Scrub one string. Returns `(scrubbed, labels that fired)`.

    `extra_patterns` are user-supplied regexes, compiled here so a bad one fails
    loudly at call time instead of silently matching nothing.
    """
    found: list[str] = []
    patterns: list[tuple[str, re.Pattern[str]]] = list(PATTERNS)
    if redact_emails:
        patterns.append(EMAIL_PATTERN)
    for i, raw in enumerate(extra_patterns or []):
        try:
            patterns.append((f"extra_{i}", re.compile(raw)))
        except re.error as exc:
            raise ValueError(f"bad --redact-extra regex #{i} ({raw!r}): {exc}") from exc
    for label, pattern in patterns:
        scrubbed, hits = pattern.subn(f"[REDACTED:{label}]", text)
        if hits:
            found.append(label)
            text = scrubbed
    return text, found


def redact_state(
    state,
    *,
    redact_emails: bool = False,
    extra_patterns: list[str] | None = None,
) -> tuple[object, list[str]]:
    """Scrub every string in a state (str, dict, list — recursively).

    Keys are left alone: a key named `password` is structure the question may
    legitimately point at (`` `state.credentials.password` ``); only *values*
    leave the machine scrubbed. Returns `(new state, labels that fired)`.
    """
    if isinstance(state, str):
        return redact_text(
            state, redact_emails=redact_emails, extra_patterns=extra_patterns)
    if isinstance(state, dict):
        out = {}
        labels: list[str] = []
        for key, value in state.items():
            new_value, hit = redact_state(
                value, redact_emails=redact_emails, extra_patterns=extra_patterns)
            out[key] = new_value
            labels.extend(hit)
        return out, labels
    if isinstance(state, list):
        out = []
        labels = []
        for value in state:
            new_value, hit = redact_state(
                value, redact_emails=redact_emails, extra_patterns=extra_patterns)
            out.append(new_value)
            labels.extend(hit)
        return out, labels
    return state, []
