"""Pure scoring and streak rules, shared by the API and the iOS app spec.

Kept free of AWS imports so it can be unit-tested directly.
"""
from __future__ import annotations

from datetime import date, timedelta

MAX_HEARTS = 5
LESSON_BONUS_XP = 5
XP_PER_FIRST_TRY = 2
PERFECT_HEARTS_BONUS_XP = 5


def lesson_xp(first_try_correct: int, hearts_left: int, passed: bool) -> int:
    """XP for one finished lesson. A failed lesson (out of hearts) earns nothing."""
    if not passed:
        return 0
    bonus = PERFECT_HEARTS_BONUS_XP if hearts_left == MAX_HEARTS else 0
    return LESSON_BONUS_XP + XP_PER_FIRST_TRY * first_try_correct + bonus


def lesson_stars(first_try_correct: int, unique_items: int, passed: bool) -> int:
    """0-3 stars from first-try accuracy."""
    if not passed or unique_items <= 0:
        return 0
    acc = first_try_correct / unique_items
    if acc >= 0.95:
        return 3
    if acc >= 0.75:
        return 2
    return 1


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
