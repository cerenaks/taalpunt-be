"""Per-question mastery and spaced review. Same rule as the app's Mastery.swift (FR-002).

- Known: the last first-try answer was correct. A first-try miss makes it not known again.
- Spaced review (Leitner boxes): a known question sits in box 1-5 and is due again after
  1, 3, 7, 16 or 35 days. A correct answer when it's due moves it up a box; a correct answer
  before it's due changes nothing (cramming doesn't count); a miss starts it over.

Kept free of AWS imports so it can be unit-tested directly.
"""
from __future__ import annotations

import re
from datetime import date

# Lesson questions ("w1-03") and, since FR-004, vocabulary words ("wonen-v07") get mastery. Only questions
# count towards readiness and the daily number; matching rounds get none.
QUESTION_ID = re.compile(r"^[a-z0-9]{1,12}-\d{2}$")
WORD_ID = re.compile(r"^[a-z0-9]{1,20}-v\d{2}$")
RANK = {"new": 0, "learning": 1, "known": 2}
MAX_WRONG_PICKS = 3
# Days until the next review, by box (index 0 = not known).
INTERVALS = (0, 1, 3, 7, 16, 35)
MAX_BOX = len(INTERVALS) - 1


def is_question(item_id: str) -> bool:
    return bool(QUESTION_ID.match(item_id))


def is_tracked(item_id: str) -> bool:
    """Questions and words keep a mastery row."""
    return bool(QUESTION_ID.match(item_id) or WORD_ID.match(item_id))


def normalize(item: dict) -> tuple[str, str | None, int]:
    """(state, last_correct, box) for a stored row. Rows from the first FR-002 version (two-day rule, no box)
    are read with the current rule: learning with a correct day is known, known had two correct days."""
    state = item.get("state", "new")
    last_correct = item.get("last_correct") or None
    box = item.get("box")
    if box is None:
        if state == "known":
            box = 2
        elif state == "learning" and last_correct:
            state, box = "known", 1
        else:
            box = 0
    return state, last_correct, int(box)


def next_state(state: str, last_correct: str | None, box: int, correct: bool, day: str) -> tuple[str, str | None, int]:
    """(state, last_correct, box) after one first-try answer on `day` (yyyy-mm-dd)."""
    if not correct:
        return "learning", None, 0
    if state == "known" and last_correct:
        gap = (date.fromisoformat(day) - date.fromisoformat(last_correct)).days
        if gap >= INTERVALS[box]:
            return "known", day, min(box + 1, MAX_BOX)
        return "known", last_correct, box
    return "known", day, 1


def is_progress(before: str, after: str, correct: bool) -> bool:
    """A question counts as newly known when a correct answer moves it up to known."""
    return correct and RANK[after] > RANK[before]
