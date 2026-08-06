"""Small, versioned pricing table for estimates from local session logs.

Prices are USD per million tokens.  They are deliberately conservative model
family matches rather than a claim that the result is an invoice.  Callers
must label values produced here as estimates.
"""

from __future__ import annotations

from dataclasses import dataclass


PRICE_TABLE_VERSION = "2026-07-09"


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float
    cache_write: float


# Most-specific prefixes must precede broad family prefixes.
_PRICES: dict[str, tuple[tuple[str, ModelPrice], ...]] = {
    "claude": (
        # https://platform.claude.com/docs/en/about-claude/pricing
        ("claude-fable-5", ModelPrice(10.0, 50.0, 1.0, 12.50)),
        ("claude-mythos-5", ModelPrice(10.0, 50.0, 1.0, 12.50)),
        ("claude-sonnet-5", ModelPrice(2.0, 10.0, 0.20, 2.50)),
        ("claude-opus-4-8", ModelPrice(5.0, 25.0, 0.50, 6.25)),
        ("claude-opus-4-7", ModelPrice(5.0, 25.0, 0.50, 6.25)),
        ("claude-opus-4-6", ModelPrice(5.0, 25.0, 0.50, 6.25)),
        ("claude-opus-4-5", ModelPrice(5.0, 25.0, 0.50, 6.25)),
        ("claude-opus-4", ModelPrice(15.0, 75.0, 1.50, 18.75)),
        ("claude-sonnet-4", ModelPrice(3.0, 15.0, 0.30, 3.75)),
        ("claude-haiku-4-5", ModelPrice(1.0, 5.0, 0.10, 1.25)),
        ("claude-3-5-haiku", ModelPrice(0.80, 4.0, 0.08, 1.0)),
        ("claude-3-5-sonnet", ModelPrice(3.0, 15.0, 0.30, 3.75)),
    ),
    "codex": (
        # https://developers.openai.com/api/docs/models and model pages.
        ("gpt-5.5", ModelPrice(5.0, 30.0, 0.50, 5.0)),
        ("gpt-5.4-pro", ModelPrice(30.0, 180.0, 30.0, 30.0)),
        ("gpt-5.4-mini", ModelPrice(0.75, 4.50, 0.075, 0.75)),
        ("gpt-5.4", ModelPrice(2.50, 15.0, 0.25, 2.50)),
        ("gpt-5.3", ModelPrice(1.75, 14.0, 0.175, 1.75)),
        ("gpt-5.2-pro", ModelPrice(21.0, 168.0, 21.0, 21.0)),
        ("gpt-5.2", ModelPrice(1.75, 14.0, 0.175, 1.75)),
        ("gpt-5.1", ModelPrice(1.25, 10.0, 0.125, 1.25)),
        ("gpt-5", ModelPrice(1.25, 10.0, 0.125, 1.25)),
        ("o3", ModelPrice(2.0, 8.0, 0.50, 2.0)),
        ("o4-mini", ModelPrice(1.10, 4.40, 0.275, 1.10)),
    ),
}


def price_for(provider: str, model: str | None) -> ModelPrice | None:
    """Return a family price, or ``None`` rather than guessing."""
    normalized = (model or "").lower().replace("_", "-")
    for prefix, price in _PRICES.get(provider, ()):
        if normalized.startswith(prefix):
            return price
    return None


def estimate_cost(provider: str, model: str | None, tokens: dict[str, int]) -> float | None:
    price = price_for(provider, model)
    if price is None:
        return None
    # OpenAI's input count includes cached input; Anthropic's does not.
    raw_input = max(0, int(tokens.get("input", 0)))
    cached = max(0, int(tokens.get("cache_read", 0)))
    uncached_input = max(0, raw_input - cached) if provider == "codex" else raw_input
    output = int(tokens.get("output", 0))
    # GPT-5.4 standard/flex sessions over 272K input are billed at 2x input
    # and 1.5x output. Local turn deltas are the closest available boundary;
    # the caller still labels every table-derived amount as estimated.
    if provider == "codex" and (model or "").lower().startswith("gpt-5.4") and raw_input > 272_000:
        uncached_input *= 2
        cached *= 2
        output = round(output * 1.5)
    dollars = (
        uncached_input * price.input
        + output * price.output
        + cached * price.cache_read
        + int(tokens.get("cache_write", 0)) * price.cache_write
    ) / 1_000_000
    return dollars
