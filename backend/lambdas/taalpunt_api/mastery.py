"""Per-question mastery: new → learning → known. Same rule as the app's Mastery.swift (FR-002).

A question becomes known after first-try correct answers on two different days, with no first-try
miss in between. A first-try miss sends it back to learning and the count starts again.
Kept free of AWS imports so it can be unit-tested directly.
"""
from __future__ import annotations

import re

# Lesson questions ("w1-03"). Vocabulary words ("wonen-v07") and matching rounds don't get mastery yet.
QUESTION_ID = re.compile(r"^[a-z0-9]{1,12}-\d{2}$")
RANK = {"new": 0, "learning": 1, "known": 2}
MAX_WRONG_PICKS = 3


def is_question(item_id: str) -> bool:
    return bool(QUESTION_ID.match(item_id))


def next_state(state: str, last_correct: str | None, correct: bool, day: str) -> tuple[str, str | None]:
    """State and last first-try-correct day after one first-try answer on `day` (yyyy-mm-dd)."""
    if not correct:
        return "learning", None
    if state == "known":
        return "known", day
    if state == "learning" and last_correct and last_correct != day:
        return "known", day
    return "learning", day


def is_progress(before: str, after: str, correct: bool) -> bool:
    """A question counts as learned today when a correct answer moves it up (new → learning, learning → known)."""
    return correct and RANK[after] > RANK[before]
