"""
Canonical candle timeframes used across the trading domain.

Callers (tools, agent, chart renderers) always use these canonical strings.
Each broker adapter is responsible for mapping to/from its own native
granularity format — the canonical set is never passed to a broker API
directly.
"""

CANONICAL_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")


def validate_timeframe(timeframe: str) -> str:
    """Normalize and validate a timeframe string against the canonical set.

    Raises ValueError with the list of supported values if unsupported —
    callers should surface this directly rather than guessing a fallback.
    """
    normalized = timeframe.strip().upper()
    if normalized not in CANONICAL_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Supported: {', '.join(CANONICAL_TIMEFRAMES)}")
    return normalized
