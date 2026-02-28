"""Shared retry helpers for transient failures."""

from __future__ import annotations

import random


def backoff_delay_seconds(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: float,
) -> float:
    """Exponential backoff with jitter for retry attempt (1-based)."""
    exponential = base_delay * (2 ** max(0, attempt - 1))
    clamped = min(exponential, max_delay)
    return float(max(0.0, clamped + random.uniform(0.0, max(0.0, jitter))))
