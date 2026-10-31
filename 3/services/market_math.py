"""Pure mathematical helpers for the virtual market engine."""


def clamp(value: float, lower: float, upper: float) -> float:
    """Keep a numeric value within inclusive lower and upper bounds."""
    return max(lower, min(upper, value))
