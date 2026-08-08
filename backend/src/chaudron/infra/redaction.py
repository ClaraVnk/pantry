"""Keep API keys out of everything that leaves the adapter.

Finding SEC-003 of the security review: a provider SDK can embed the credential it
was called with in its own exception message, and `str(exc)` in a log line, a
traceback or an error response is enough to leak a third party's billable secret.
The rule the adapters follow is therefore *translate, never quote*: a domain error
is built from our own fields.

This module is the second line of defence, for the one place where quoting is
genuinely useful -- a snippet of a malformed model response, which helps diagnose a
bad prompt. That snippet is untrusted text, so it is scrubbed here before it can
reach a message.

It is also the last one: :class:`chaudron.infra.logging.JsonFormatter` runs
:func:`redact` over every line it writes. That makes the patterns below an
invariant of the transport rather than a discipline of each caller -- one call site
that forgets ``secrets=`` no longer decides whether a credential reaches a log file.
The literal ``secrets=`` path is still the stronger one, because it catches keys of
shapes nobody published; the patterns are what covers the sites that cannot know the
secret they are holding.

One thing here is not a credential and belongs anyway: PostgreSQL's ``DETAIL``
field, which answers a constraint violation by quoting the offending row back --
every column of it, composed by the server rather than by the driver, and therefore
untouched by the ``hide_parameters=True`` that ``infra/db.py`` sets against exactly
this. It has a fixed shape, so it can be recognised here; that is the only reason it
is here rather than left to a discipline at each call site, which is the arrangement
this module exists to replace.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

__all__ = ["REDACTED", "redact", "snippet"]

REDACTED: Final = "[redacted]"

#: Shapes that are secrets by construction. Deliberately broad: a false positive
#: costs a slightly less readable diagnostic, a false negative costs a rotation.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Anthropic, OpenAI and most of their imitators.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    # Google AI Studio.
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}"),
    # AWS access key ids, which show up in misconfigured gateways.
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    # Bearer tokens and JWTs.
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{4,}"),
    # HTTP Basic, whose base64 hides both halves from every other pattern here --
    # which is how the calendar feed's secret travels. Sixteen base64 characters
    # minimum, so the prose "Basic authentication is required" is not mistaken for
    # a credential (the longest word after "Basic" that survives is fifteen).
    re.compile(r"\bBasic\s+[A-Za-z0-9+/]{16,}={0,2}", re.IGNORECASE),
    # Credentials embedded in a URL.
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    # PEM blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

#: Shortest run of letters and digits that could be an unprefixed credential.
#: Mistral AI issues 32, the calendar feed's secret renders as 32, a hex digest
#: token is 40. Anything shorter is dense enough with real identifiers that
#: blanking it would cost more diagnostics than it saves rotations.
_MIN_OPAQUE_TOKEN_LENGTH: Final = 32

#: Every unbroken alphanumeric run of that length or more, blanked on **length
#: alone**.
#:
#: It used to demand a shape as well -- forty hex characters, unpadded upper-case
#: base32, or all three character classes at once -- and the probabilistic argument
#: for the third ("a randomly drawn key misses a class with probability below
#: 1e-7") held only for a mixed-case alphabet. Plenty of vendors issue keys that
#: are lower-case hex or lower-case alphanumeric, and every one of those survived
#: the filter intact: thirty-two lower-case letters, thirty-two lower-case hex
#: characters and thirty-two lower-case alphanumerics were all passed through
#: verbatim. That is precisely the invariant this layer owes the call sites that do
#: not know what they are holding, so the shape test is gone. Once lower-case-only
#: runs are covered, *every* partition of the alphabet is covered (an upper-case
#: run is base32, a mixed run has two classes), so keeping the classifier would
#: have been a function that always returns ``True`` dressed up as a decision.
#:
#: **What that also blanks**, and the cost is diagnostic readability rather than
#: correctness:
#:
#: * a UUID written without its dashes (32 hex characters) -- one written *with*
#:   them is untouched, and that is the form every log line in this application
#:   emits (``household_id``, ``request_id``, ``incident``);
#: * a SHA-256 digest (64 hex) and a git commit (40), so ``receipt.image_sha256``
#:   would not survive a log line;
#: * a long base64 fragment of a request or response body;
#: * a run of 32 or more letters with no separator in it -- no identifier in this
#:   schema is one, because ``_``, ``-``, ``.`` and ``/`` all break a run:
#:   ``ck_inventory_lot_quantity_positive`` is seven runs, the longest nine
#:   characters, and ``mistral-small-latest`` is three.
#:
#: The calendar feed *identifier* is still spared, and by the one property that
#: never depended on shape: it renders as 26 characters, under the bound.
#:
#: The pattern stays a single non-backtracking scan -- no lookahead, no per-match
#: callback. The lookahead form this replaced (``(?=[A-Za-z0-9]*[a-z])`` and its
#: two siblings) rescanned the run from every position it failed at, which is
#: quadratic in the run's length; log text is partly attacker-influenced (a
#: poisoned catalogue label, a model's answer), so a 100 kB alphanumeric blob in
#: an ``extra=`` would have turned one log line into hundreds of milliseconds.
_OPAQUE_TOKEN_RUN: Final = re.compile(
    rf"(?<![A-Za-z0-9])[A-Za-z0-9]{{{_MIN_OPAQUE_TOKEN_LENGTH},}}(?![A-Za-z0-9])"
)


def _redact_opaque_tokens(text: str) -> str:
    return _OPAQUE_TOKEN_RUN.sub(REDACTED, text)


#: PostgreSQL hands the offending row straight back in the ``DETAIL`` field of a
#: constraint violation, and asyncpg appends that field to ``str(exc)`` -- so
#: ``api/errors.py``, which logs every unhandled exception with ``exc_info``, wrote
#: the whole row to disk. ``infra/db.py`` sets ``hide_parameters=True``, which
#: suppresses SQLAlchemy's *bound parameters* and does nothing at all about this:
#: the text is composed by the server, not by the driver.
#:
#: The two forms that carry values, and what is kept of each:
#:
#: * ``Failing row contains (250.000, EUR, 2026-01-04, <location>, <household>)``
#:   -- emitted for a CHECK and for a NOT NULL violation, and it is *every column*
#:   of the row. Nothing of it is kept.
#: * ``Key (household_id, gtin)=(<uuid>, 3017620422003) already exists.`` --
#:   emitted for a UNIQUE and a foreign-key violation. The column *names* are kept
#:   because they are what identifies the constraint to an operator; the values are
#:   not.
#:
#: What survives either way is the sentence in front of the ``DETAIL``, which names
#: the relation and the violated constraint -- the whole of what a fix is made
#: from. Both patterns are anchored to a single line and use nothing but negated
#: character classes, so they cannot backtrack.
_POSTGRES_ROW_ECHO: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"Failing row contains \([^\n]*"), f"Failing row contains {REDACTED}"),
    (re.compile(r"\bKey \(([^)\n]*)\)=[^\n]*"), rf"Key (\1)={REDACTED}"),
)


def _redact_row_echo(text: str) -> str:
    for pattern, replacement in _POSTGRES_ROW_ECHO:
        text = pattern.sub(replacement, text)
    return text


#: Anything shorter than this is not a credential; blanking it would only mangle
#: readable text (and an empty configured key must never blank the whole string).
_MIN_LITERAL_SECRET_LENGTH: Final = 8


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with known and probable credentials replaced.

    ``secrets`` are the values we actually hold -- the household's decrypted key,
    typically. They are removed by literal match, which catches a key that no
    pattern would recognise (a self-hosted gateway's opaque token, for instance).
    """
    for secret in secrets:
        if len(secret) >= _MIN_LITERAL_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _redact_opaque_tokens(_redact_row_echo(text))


def snippet(text: str, *, limit: int = 200, secrets: Iterable[str] = ()) -> str:
    """A short, scrubbed, single-line excerpt safe to put in a domain error."""
    cleaned = redact(" ".join(text.split()), secrets=secrets)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "..."
