"""Request validation for POST /me/sessions. No AWS imports."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .scoring import MAX_HEARTS

KINDS = {"lesson", "vocab", "test", "mix", "mistakes"}
# Lessons that keep a star rating on the path. Mixed and mistake rounds do not.
RATED_KINDS = {"lesson", "vocab", "test"}
MAX_ANSWERS = 40

_ITEM_ID = re.compile(r"^(?:[a-z0-9]{1,12}-\d{2}|[a-z0-9]{1,20}-v\d{2}|match)$")
_LESSON_ID = {
    "lesson": re.compile(r"^[a-z0-9]{1,12}$"),
    "vocab": re.compile(r"^voc-[a-z0-9]{1,20}$"),
    "test": re.compile(r"^test-[a-z0-9]{1,20}$"),
    "mix": re.compile(r"^mix$"),
    "mistakes": re.compile(r"^mistakes$"),
}


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Answer:
    item_id: str
    correct: bool
    retry: bool


@dataclass(frozen=True)
class Session:
    session_id: str
    kind: str
    lesson_id: str
    local_date: date
    hearts_left: int
    completed: bool
    answers: tuple[Answer, ...]

    @property
    def unique_items(self) -> int:
        return sum(1 for a in self.answers if not a.retry)

    @property
    def first_try_correct(self) -> int:
        return sum(1 for a in self.answers if a.correct and not a.retry)

    @property
    def passed(self) -> bool:
        return self.completed and self.hearts_left > 0


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValidationError(msg)


def parse_session(body: object, now: datetime | None = None) -> Session:
    _require(isinstance(body, dict), "Body must be a JSON object.")
    assert isinstance(body, dict)

    sid = body.get("session_id")
    _require(isinstance(sid, str), "session_id is required.")
    try:
        sid = str(uuid.UUID(sid))
    except ValueError as exc:
        raise ValidationError("session_id must be a UUID.") from exc

    kind = body.get("kind")
    _require(kind in KINDS, f"kind must be one of {sorted(KINDS)}.")

    lesson_id = body.get("lesson_id")
    _require(isinstance(lesson_id, str) and bool(_LESSON_ID[kind].match(lesson_id)), "lesson_id does not match kind.")

    raw_day = body.get("local_date")
    _require(isinstance(raw_day, str), "local_date (YYYY-MM-DD) is required.")
    try:
        local_day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise ValidationError("local_date must be YYYY-MM-DD.") from exc
    utc_today = (now or datetime.now(timezone.utc)).date()
    # Time zones run from UTC-12 to UTC+14, so a real local date is never more than a day off.
    _require(abs((local_day - utc_today).days) <= 1, "local_date is too far from the current date.")

    hearts = body.get("hearts_left")
    _require(isinstance(hearts, int) and not isinstance(hearts, bool) and 0 <= hearts <= MAX_HEARTS,
             f"hearts_left must be 0-{MAX_HEARTS}.")

    completed = body.get("completed")
    _require(isinstance(completed, bool), "completed must be true or false.")

    raw_answers = body.get("answers")
    _require(isinstance(raw_answers, list) and 1 <= len(raw_answers) <= MAX_ANSWERS,
             f"answers must be a list of 1-{MAX_ANSWERS} items.")
    answers: list[Answer] = []
    for i, a in enumerate(raw_answers):
        _require(isinstance(a, dict), f"answers[{i}] must be an object.")
        item_id, correct, retry = a.get("item_id"), a.get("correct"), a.get("retry", False)
        _require(isinstance(item_id, str) and bool(_ITEM_ID.match(item_id)), f"answers[{i}].item_id is not valid.")
        _require(isinstance(correct, bool), f"answers[{i}].correct must be true or false.")
        _require(isinstance(retry, bool), f"answers[{i}].retry must be true or false.")
        answers.append(Answer(item_id, correct, retry))

    # Hearts must agree with the answers: each wrong answer outside a matching round costs one.
    wrong = sum(1 for a in answers if not a.correct and a.item_id != "match")
    _require(hearts == max(0, MAX_HEARTS - wrong), "hearts_left does not match the answers.")
    _require(completed or hearts == 0, "An unfinished session must have run out of hearts.")

    return Session(sid, kind, lesson_id, local_day, hearts, completed, tuple(answers))
