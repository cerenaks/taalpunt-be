"""Pure scoring and streak rules, shared by the API and the iOS app spec.

Kept free of AWS imports so it can be unit-tested directly.
"""
from __future__ import annotations

from datetime import date, timedelta

MAX_HEARTS = 5
LESSON_BONUS_XP = 5
XP_PER_FIRST_TRY = 2
PERFECT_HEARTS_BONUS_XP = 5
MAX_STARS = 3


def lesson_xp(first_try_correct: int, hearts_left: int, passed: bool) -> int:
    """XP for one finished lesson. A failed lesson (out of hearts) earns nothing."""
    if not passed:
        return 0
    bonus = PERFECT_HEARTS_BONUS_XP if hearts_left == MAX_HEARTS else 0
    return LESSON_BONUS_XP + XP_PER_FIRST_TRY * first_try_correct + bonus


def lesson_stars(completions: int) -> int:
    """Stars for a lesson: one for every passed run, up to 3 (the Duolingo way)."""
    return max(0, min(MAX_STARS, completions))


def next_streak(current: int, last_day: date | None, today: date) -> int:
    """Streak after earning XP on `today`."""
    if last_day == today:
        return max(current, 1)
    if last_day == today - timedelta(days=1):
        return current + 1
    return 1


def visible_streak(current: int, last_day: date | None, today: date) -> int:
    """Streak to show: it lapses if neither today nor yesterday had activity."""
    if last_day in (today, today - timedelta(days=1)):
        return current
    return 0
