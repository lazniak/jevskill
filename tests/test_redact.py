"""Tests for credential redaction before state leaves the machine.

The skill sends the user's logs, diffs and tickets to a third party by design.
These tests pin the promise that the *default* is to scrub credential-shaped
strings, that what was scrubbed is reported rather than silent, and that the
escape hatch (`--no-redact`) exists for the run where the secret is the subject.
"""

from __future__ import annotations

import pytest

from jevskill import redact
from jevskill.redact import redact_state, redact_text

SECRET = "sk-or-v1-0123456789abcdef0123456789abcdef"

#: `(label, text, the secret that must not survive)`. Shared with the bundled
#: script's test so the two copies of the pattern list cannot drift apart.
SAMPLES = [
    ("openrouter_key",
     "key sk-or-v1-0123456789abcdef0123456789abcdef in log",
     "sk-or-v1-0123456789abcdef0123456789abcdef"),
    ("api_key", "Authorization: sk-abc123def456ghi789", "sk-abc123def456ghi789"),
    ("aws_key", "credentials AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("github_token", "token ghp_0123456789abcdefghijklmnopqrstuvwxyz12",
     "ghp_0123456789abcdefghijklmnopqrstuvwxyz12"),
    ("slack_token", "xoxb-123456789-abcdef", "xoxb-123456789-abcdef"),
    ("bearer", "Authorization: Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
    ("password_param", "login failed for password=hunter2 retry", "hunter2"),
    ("password_param", "request used api_key = 0123456789abcdef", "0123456789abcdef"),
    ("password_param", "callback had ?token=abc123def456", "abc123def456"),
    ("jwt",
     "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
     "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"),
]


@pytest.mark.parametrize(("label", "text", "secret"), SAMPLES)
def test_credential_patterns_are_scrubbed_and_labelled(label, text, secret):
    scrubbed, labels = redact_text(text)
    assert label in labels
    assert f"[REDACTED:{label}]" in scrubbed
    assert secret not in scrubbed, "the raw secret itself must be gone"


def test_multiline_private_key_is_scrubbed_whole():
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK\nmore\n-----END RSA PRIVATE KEY-----\nafter"
    scrubbed, labels = redact_text(text)
    assert labels == ["private_key"]
    assert "MIIEpAIBAAK" not in scrubbed
    assert scrubbed.startswith("before\n") and scrubbed.endswith("after")


def test_the_specific_openrouter_pattern_wins_over_the_generic_key():
    _, labels = redact_text(SECRET)
    assert labels == ["openrouter_key"], "order in PATTERNS is load-bearing"


def test_emails_stay_by_default_and_are_opt_in():
    text = "write to anna@example.com about the outage"
    assert redact_text(text)[0] == text
    scrubbed, labels = redact_text(text, redact_emails=True)
    assert labels == ["email"] and "anna" not in scrubbed


def test_clean_text_is_untouched_and_reports_nothing():
    text = "ERROR payment capture failed after 3 retries, order A-104"
    scrubbed, labels = redact_text(text)
    assert scrubbed == text and labels == []


def test_extra_patterns_are_compiled_and_labelled():
    scrubbed, labels = redact_text("room code 4821", extra_patterns=[r"\b\d{4}\b"])
    assert labels == ["extra_0"] and "[REDACTED:extra_0]" in scrubbed


def test_a_bad_extra_regex_fails_loudly_not_silently():
    with pytest.raises(ValueError, match="bad --redact-extra regex #0"):
        redact_text("x", extra_patterns=["(["])


def test_state_is_scrubbed_recursively_but_keys_are_structure():
    state = {
        "logs": ["GET /health 200", "login used password=hunter2"],
        "credentials": {"api_key": "sk-abc123def456ghi789", "retries": 3},
        "ticket": "contact anna@example.com",
    }
    scrubbed, labels = redact_state(state)
    assert labels == ["password_param", "api_key"]
    assert scrubbed["credentials"]["api_key"] == "[REDACTED:api_key]"
    assert scrubbed["credentials"]["retries"] == 3  # non-strings pass through
    assert "credentials" in scrubbed  # the KEY is untouched: questions point at it
    assert scrubbed["ticket"] == "contact anna@example.com"  # emails are opt-in


def test_list_states_are_scrubbed():
    scrubbed, labels = redact_state(["healthcheck ok", "token=abcdef123456"])
    assert labels == ["password_param"]
    assert scrubbed[1] == "[REDACTED:password_param]"


class TestBundledScriptStaysInStep:
    """`jev_query.py` carries its own compiled copy of the patterns because the
    Skill must run with nothing installed — the same rule as `PROVIDERS`. That
    makes drift possible, so it is tested rather than trusted."""

    def test_it_redacts_everything_the_package_does(self, jev_query):
        for label, text, secret in SAMPLES:
            scrubbed, labels = jev_query.redact_value(text)
            assert label in labels, f"bundled script missed {label}"
            assert secret not in scrubbed

    def test_the_label_sets_are_identical(self, jev_query):
        assert {label for label, _ in jev_query.REDACT_PATTERNS} == {
            label for label, _ in redact.PATTERNS
        }, "the bundled copy and jevskill/redact.py have drifted"

    def test_it_scrubs_nested_state_and_keeps_keys(self, jev_query):
        scrubbed, labels = jev_query.redact_value(
            {"creds": {"api_key": "sk-abc123def456ghi789"}, "retries": 3})
        assert labels == ["api_key"]
        assert scrubbed["creds"]["api_key"] == "[REDACTED:api_key]"
        assert scrubbed["retries"] == 3

    def test_emails_are_opt_in_here_too(self, jev_query):
        text = "contact anna@example.com about the outage"
        assert jev_query.redact_value(text)[0] == text
        assert jev_query.redact_value(text, emails=True)[1] == ["email"]
