"""Reading token counts off a provider's answer, defensively.

Usage is *reported* data, not data we compute, and it arrives from five different
vendors in five shapes. This module holds the one rule they all go through:
**anything that is not a plain non-negative integer is "unknown", never zero.**

That rule is the whole point. A response shape that moves, a gateway that strips
the block, an older Ollama that omits a counter -- each of those is a reason not to
know, and every one of them would otherwise land in the interface as a confident
``0 jetons`` next to a suggestion the household actually paid for
(:class:`~chaudron.domain.llm_ports.TokenUsage`).
"""

from __future__ import annotations

from typing import Any

__all__ = ["subtract_cached", "token_count"]


def token_count(value: Any) -> int | None:
    """A reported counter, or ``None`` when it is absent or not a count.

    ``bool`` is excluded explicitly: it is a subclass of ``int`` in Python, and a
    provider that answered ``true`` should read as "did not say", not as one token.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def subtract_cached(prompt_tokens: int | None, cached: int | None) -> int | None:
    """Split a provider's all-in prompt count into the uncached part.

    OpenAI, Mistral AI and Gemini report a prompt total that *includes* the tokens
    they served from cache; Anthropic reports the two separately. ``TokenUsage``
    fixes one meaning -- ``input_tokens`` is what was not cached -- so the providers
    that bundle them are normalised here rather than in three adapters.

    Clamped at zero: if a provider ever reports a cached count larger than its own
    total, the subtraction is meaningless and a negative token count would be a
    louder lie than a zero. It cannot be ``None`` either, since the household did
    pay for a prompt.
    """
    if prompt_tokens is None:
        return None
    if cached is None:
        return prompt_tokens
    return max(prompt_tokens - cached, 0)
