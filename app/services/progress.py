"""
How the days have gone: streaks of days written down, and the averages a
period comes to.

A day counts as written down when it has at least one meal. Today still
being empty does not break a streak — the day is not over — so the current
streak runs up to today when today is logged, and up to yesterday otherwise.
"""

from __future__ import annotations

from datetime import date, timedelta


def current_streak(logged: set[date], today: date) -> int:
    day = today if today in logged else today - timedelta(days=1)
    count = 0
    while day in logged:
        count += 1
        day -= timedelta(days=1)
    return count


def longest_streak(logged: set[date]) -> int:
    best = 0
    for day in logged:
        # Only count forward from the first day of a run
        if day - timedelta(days=1) in logged:
            continue
        length = 1
        while day + timedelta(days=length) in logged:
            length += 1
        best = max(best, length)
    return best


def average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None
