"""Free and Premium content (FR-001). Pure functions, shared by the CDK deploy and the API Lambda.

- Each lesson in knm.json has "tier": "free" or "premium" (missing = free).
- A theme's vocabulary round ("voc-<unit>") and practice test ("test-<unit>") are free only when every
  lesson in the theme is free. Today that's Wonen.
- The free bundle keeps every theme and lesson title (so the app can show locked stops) but empties the
  questions of Premium lessons and the vocabulary of themes with Premium lessons.
"""
from __future__ import annotations

import copy
import re

WORD_ID = re.compile(r"^([a-z0-9]{1,20})-v\d{2}$")
QUESTION_ID = re.compile(r"^([a-z0-9]{1,12})-\d{2}$")


def is_premium_lesson(lesson: dict) -> bool:
    return lesson.get("tier", "free") == "premium"


def premium_ids(doc: dict) -> tuple[set[str], set[str]]:
    """(lesson ids that need Premium, incl. voc-/test- rounds; theme ids whose words need Premium)."""
    lessons: set[str] = set()
    units: set[str] = set()
    for unit in doc["units"]:
        locked = [l["id"] for l in unit["lessons"] if is_premium_lesson(l)]
        lessons.update(locked)
        if locked:
            units.add(unit["id"])
            lessons.update({f"voc-{unit['id']}", f"test-{unit['id']}"})
    return lessons, units


def free_bundle(doc: dict) -> dict:
    """The public bundle: Premium questions and words taken out, everything else as it is."""
    out = copy.deepcopy(doc)
    _, premium_units = premium_ids(doc)
    for unit in out["units"]:
        for lesson in unit["lessons"]:
            if is_premium_lesson(lesson):
                lesson["questions"] = []
        if unit["id"] in premium_units:
            unit["vocabulary"] = []
    return out


def needs_premium(lesson_id: str, item_ids: list[str], premium_lessons: set[str], premium_units: set[str]) -> bool:
    """True when a session touches Premium content: a Premium lesson, or any Premium question or word
    (Mix and Foutjes rounds draw from all lessons)."""
    if lesson_id in premium_lessons:
        return True
    for item_id in item_ids:
        q = QUESTION_ID.match(item_id)
        if q and q.group(1) in premium_lessons:
            return True
        w = WORD_ID.match(item_id)
        if w and w.group(1) in premium_units:
            return True
    return False
