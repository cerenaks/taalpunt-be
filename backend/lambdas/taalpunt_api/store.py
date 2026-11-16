"""DynamoDB access. One table, partitioned per user.

Key layout (PK = USER#<cognito sub>):
  SK = PROFILE              xp_total, streak, last_day; exam_date, exam_date_set_at, exam_passed_on (FR-004)
  SK = LESSON#<lesson_id>   best_stars, completions, last_completed, best_score (best first-try %, FR-004)
  SK = DAY#<yyyy-mm-dd>     xp, learned (questions that became known that day)
  SK = MISTAKE#<item_id>    wrong_count, last_wrong
  SK = ITEM#<item_id>       state (learning|known), last_correct, box (spaced review), last_seen, wrong_picks (FR-002);
                            questions, and words since FR-004
  SK = SAVED#<item_id>      saved_at: My list (FR-004), questions and words the learner saved
  SK = EXAM#<yyyy-mm-dd>    outcome (passed|retake|skipped), reported_at: how an exam went, for analytics (FR-004)
  SK = SESSION#<uuid>       summary of one finished or failed lesson (expires after a year)
  SK = ENTITLEMENT#<source> Premium (FR-001): source manual|apple, until (ISO time, absent = no end), note;
                            apple also: original_transaction_id, product_id, environment, signed_date (ms)

PK = APPLE#<original transaction id>, SK = OWNER: which learner an App Store subscription belongs to (sub)
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from .mastery import MAX_WRONG_PICKS, is_progress, is_question, is_tracked, next_state, normalize
from .scoring import lesson_stars, lesson_xp, next_streak, visible_streak
from .validation import RATED_KINDS, Session

SESSION_TTL_SECONDS = 365 * 24 * 3600
# Days of XP history in /me/progress: enough for the app's streak calendar to show about four months.
DAY_HISTORY = 120
_ser, _de = TypeSerializer(), TypeDeserializer()


def _table_name() -> str:
    return os.environ["TABLE_NAME"]


def _client():
    return boto3.client("dynamodb")


def _table():
    return boto3.resource("dynamodb").Table(_table_name())


def _pk(sub: str) -> str:
    return f"USER#{sub}"


def _plain(v: Any) -> Any:
    """Decimal -> int for JSON responses."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if hasattr(v, "to_integral_value"):
        return int(v)
    return v


def _query_prefix(sub: str, prefix: str, newest_first: bool = False, limit: int | None = None) -> list[dict]:
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_pk(sub)) & Key("SK").begins_with(prefix),
        "ScanIndexForward": not newest_first,
    }
    if limit:
        kwargs["Limit"] = limit
    items: list[dict] = []
    table = _table()
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if limit or "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def get_progress(sub: str, today: date) -> dict:
    table = _table()
    profile = table.get_item(Key={"PK": _pk(sub), "SK": "PROFILE"}).get("Item") or {}
    last_day = date.fromisoformat(profile["last_day"]) if profile.get("last_day") else None
    lessons = {
        i["SK"].split("#", 1)[1]: {
            # Derived from completions so older rows (accuracy-based stars) follow the same rule.
            "best_stars": lesson_stars(int(i.get("completions", 0))),
            "completions": i.get("completions", 0),
            "last_completed": i.get("last_completed"),
            "best_score": i.get("best_score", 0),
        }
        for i in _query_prefix(sub, "LESSON#")
    }
    # My list (FR-004): the questions and words the learner saved, oldest first.
    saved = [i["SK"].split("#", 1)[1] for i in sorted(_query_prefix(sub, "SAVED#"), key=lambda i: i.get("saved_at", ""))]
    days = [{"date": i["SK"].split("#", 1)[1], "xp": i.get("xp", 0), "learned": i.get("learned", 0)}
            for i in _query_prefix(sub, "DAY#", newest_first=True, limit=DAY_HISTORY)]
    today_day = next((d for d in days if d["date"] == today.isoformat()), {})
    mistakes = _query_prefix(sub, "MISTAKE#")
    # Compact on purpose: about 600 questions → a few KB. [state, last correct day or "", review box],
    # all strings so clients can read every entry as a list of text.
    items = {}
    for i in _query_prefix(sub, "ITEM#"):
        state, last_correct, box = normalize(i)
        items[i["SK"].split("#", 1)[1]] = [state, last_correct or "", str(box)]
    return _plain({
        "xp_total": profile.get("xp_total", 0),
        "streak": visible_streak(int(profile.get("streak", 0)), last_day, today),
        "last_day": profile.get("last_day"),
        "today_xp": today_day.get("xp", 0),
        "today_learned": today_day.get("learned", 0),
        "lessons": lessons,
        "items": items,
        "premium": get_premium(sub),
        "days": days,
        "mistakes_count": len(mistakes),
        "saved": saved,
        "exam": {"date": profile.get("exam_date"), "passed_on": profile.get("exam_passed_on")},
    })


EXAM_OUTCOMES = ("passed", "retake", "skipped")


def set_exam_date(sub: str, exam_date: str | None, now: datetime | None = None) -> None:
    """The learner's exam date (FR-004), or None when they removed it."""
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    key = {"PK": _pk(sub), "SK": "PROFILE"}
    if exam_date:
        _table().update_item(Key=key, UpdateExpression="SET exam_date = :d, exam_date_set_at = :ts",
                             ExpressionAttributeValues={":d": exam_date, ":ts": ts})
    else:
        _table().update_item(Key=key, UpdateExpression="REMOVE exam_date SET exam_date_set_at = :ts",
                             ExpressionAttributeValues={":ts": ts})


def report_exam(sub: str, exam_date: str, outcome: str, now: datetime | None = None) -> None:
    """How the exam on `exam_date` went (FR-004): kept per exam, so retakes stay visible. "passed" is also
    kept on the profile."""
    if outcome not in EXAM_OUTCOMES:
        raise ValueError(f"outcome must be one of {EXAM_OUTCOMES}")
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    table = _table()
    table.put_item(Item={"PK": _pk(sub), "SK": f"EXAM#{exam_date}", "outcome": outcome, "reported_at": ts})
    if outcome == "passed":
        table.update_item(Key={"PK": _pk(sub), "SK": "PROFILE"},
                          UpdateExpression="SET exam_passed_on = :d REMOVE exam_date",
                          ExpressionAttributeValues={":d": exam_date})
    elif outcome == "skipped":
        table.update_item(Key={"PK": _pk(sub), "SK": "PROFILE"}, UpdateExpression="REMOVE exam_date")


def set_saved(sub: str, item_id: str, saved: bool, now: datetime | None = None) -> None:
    """Adds an item to My list (FR-004) or takes it off. Saving twice keeps the first time."""
    table = _table()
    key = {"PK": _pk(sub), "SK": f"SAVED#{item_id}"}
    if not saved:
        table.delete_item(Key=key)
        return
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    try:
        table.put_item(Item={**key, "saved_at": ts}, ConditionExpression="attribute_not_exists(SK)")
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


PREMIUM_SOURCES = ("manual", "apple")


def get_premium(sub: str, now: datetime | None = None) -> dict:
    """Premium status from all sources: active if any entitlement has no end or ends in the future."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    active_until: list[str | None] = []
    sources: list[str] = []
    for item in _query_prefix(sub, "ENTITLEMENT#"):
        until = item.get("until")
        if until is None or until > now_iso:
            active_until.append(until)
            sources.append(item.get("source", item["SK"].split("#", 1)[1]))
    if not active_until:
        return {"active": False, "until": None, "sources": []}
    forever = any(u is None for u in active_until)
    return {"active": True, "until": None if forever else max(active_until), "sources": sorted(set(sources))}


def grant_premium(sub: str, source: str, until: str | None = None, note: str = "",
                  now: datetime | None = None) -> None:
    """Gives Premium from one source. `until` is an ISO time; None means no end."""
    if source not in PREMIUM_SOURCES:
        raise ValueError(f"source must be one of {PREMIUM_SOURCES}")
    item: dict[str, Any] = {
        "PK": _pk(sub), "SK": f"ENTITLEMENT#{source}", "source": source,
        "granted_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }
    if until:
        item["until"] = until
    if note:
        item["note"] = note
    _table().put_item(Item=item)


def revoke_premium(sub: str, source: str) -> None:
    _table().delete_item(Key={"PK": _pk(sub), "SK": f"ENTITLEMENT#{source}"})


# ---------- App Store subscriptions (FR-001 step 2) ----------

def _apple_pk(original_transaction_id: str) -> str:
    return f"APPLE#{original_transaction_id}"


def apple_owner(original_transaction_id: str) -> str | None:
    """The learner an App Store subscription is linked to, or None before the first purchase report."""
    item = _table().get_item(Key={"PK": _apple_pk(original_transaction_id), "SK": "OWNER"}).get("Item")
    return item["sub"] if item else None


def set_apple_owner(original_transaction_id: str, sub: str, now: datetime | None = None) -> None:
    _table().put_item(Item={
        "PK": _apple_pk(original_transaction_id), "SK": "OWNER", "sub": sub,
        "updated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    })


def put_apple_entitlement(sub: str, *, until: str, original_transaction_id: str, transaction_id: str,
                          product_id: str, environment: str, signed_date: int,
                          now: datetime | None = None) -> bool:
    """Stores the state of the learner's App Store subscription. Apple's notifications can arrive out of
    order, so an update signed before the stored one is ignored. Returns False when it was ignored."""
    item = {
        "PK": _pk(sub), "SK": "ENTITLEMENT#apple", "source": "apple", "until": until,
        "original_transaction_id": original_transaction_id, "transaction_id": transaction_id,
        "product_id": product_id, "environment": environment, "signed_date": signed_date,
        "granted_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(signed_date) OR signed_date <= :sd",
                          ExpressionAttributeValues={":sd": signed_date})
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def end_apple_entitlement(sub: str, original_transaction_id: str, now: datetime | None = None) -> None:
    """Ends Premium from this subscription for a learner it moved away from (restored on another account)."""
    try:
        _table().update_item(
            Key={"PK": _pk(sub), "SK": "ENTITLEMENT#apple"},
            UpdateExpression="SET #u = :now",
            ConditionExpression="original_transaction_id = :otid",
            ExpressionAttributeNames={"#u": "until"},
            ExpressionAttributeValues={":now": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                                       ":otid": original_transaction_id},
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def get_mistakes(sub: str) -> list[dict]:
    items = _query_prefix(sub, "MISTAKE#")
    out = [{"item_id": i["SK"].split("#", 1)[1], "wrong_count": i.get("wrong_count", 1),
            "last_wrong": i.get("last_wrong")} for i in items]
    out.sort(key=lambda m: (-int(m["wrong_count"]), m["item_id"]))
    return _plain(out)


class DuplicateSession(Exception):
    def __init__(self, summary: dict):
        super().__init__("duplicate session")
        self.summary = summary


def record_session(sub: str, s: Session, now: datetime | None = None) -> dict:
    """Apply one session atomically. Retrying the same session_id returns the first result."""
    now = now or datetime.now(timezone.utc)
    pk = _pk(sub)
    client, table_name = _client(), _table_name()

    existing = client.get_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": f"SESSION#{s.session_id}"}})
    if "Item" in existing:
        item = {k: _de.deserialize(v) for k, v in existing["Item"].items()}
        raise DuplicateSession(_plain(item.get("result", {})))

    table = _table()
    profile = table.get_item(Key={"PK": pk, "SK": "PROFILE"}).get("Item") or {}
    prev_last = profile.get("last_day")
    prev_streak = int(profile.get("streak", 0))
    lesson_item = table.get_item(Key={"PK": pk, "SK": f"LESSON#{s.lesson_id}"}).get("Item") or {}

    xp = lesson_xp(s.first_try_correct, s.hearts_left, s.passed)
    rated_pass = s.kind in RATED_KINDS and s.passed
    stars = lesson_stars(int(lesson_item.get("completions", 0)) + 1) if rated_pass else 0
    earned = xp > 0
    streak = next_streak(prev_streak, date.fromisoformat(prev_last) if prev_last else None, s.local_date) if earned else prev_streak
    last_day = s.local_date.isoformat() if earned else prev_last
    xp_total = int(profile.get("xp_total", 0)) + xp
    ts = now.isoformat(timespec="seconds")

    # Mastery (FR-002): the first non-retry answer per question or word decides; matching rounds are skipped.
    # Only questions count as learned (words have their own progress on the Woordjes stop, FR-004).
    day = s.local_date.isoformat()
    first: dict[str, Any] = {}
    for a in s.answers:
        if not a.retry and is_tracked(a.item_id):
            first.setdefault(a.item_id, a)
    before = _get_items(client, table_name, pk, list(first))
    item_writes: list[dict] = []
    learned = 0
    for qid, a in first.items():
        prev = before.get(qid, {})
        prev_state, prev_correct, prev_box = normalize(prev)
        state, last_correct, box = next_state(prev_state, prev_correct, prev_box, a.correct, day)
        if is_question(qid) and is_progress(prev_state, state, a.correct):
            learned += 1
        picks = list(prev.get("wrong_picks", []))
        if not a.correct and a.picked:
            picks = (picks + [a.picked])[-MAX_WRONG_PICKS:]
        row = {"PK": pk, "SK": f"ITEM#{qid}", "state": state, "box": box, "last_seen": day,
               "updated_at": now.isoformat(timespec="seconds")}
        if last_correct:
            row["last_correct"] = last_correct
        if picks:
            row["wrong_picks"] = picks
        item_writes.append(row)

    result = {"xp_gained": xp, "stars": stars, "streak": streak, "xp_total": xp_total, "learned": learned}

    def put(item: dict, condition: str | None = None) -> dict:
        p: dict[str, Any] = {"TableName": table_name, "Item": {k: _ser.serialize(v) for k, v in item.items()}}
        if condition:
            p["ConditionExpression"] = condition
        return {"Put": p}

    def update(sk: str, expr: str, values: dict, condition: str | None = None, names: dict | None = None) -> dict:
        u: dict[str, Any] = {
            "TableName": table_name,
            "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
            "UpdateExpression": expr,
            "ExpressionAttributeValues": {k: _ser.serialize(v) for k, v in values.items()},
        }
        if condition:
            u["ConditionExpression"] = condition
        if names:
            u["ExpressionAttributeNames"] = names
        return {"Update": u}

    ops: list[dict] = [put({
        "PK": pk, "SK": f"SESSION#{s.session_id}", "created_at": ts, "kind": s.kind, "lesson_id": s.lesson_id,
        "local_date": s.local_date.isoformat(), "hearts_left": s.hearts_left, "completed": s.completed,
        "first_try_correct": s.first_try_correct, "unique_items": s.unique_items, "result": result,
        "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
    }, condition="attribute_not_exists(SK)")]

    if earned:
        # Guard against two sessions racing on the same profile read.
        guard = "attribute_not_exists(last_day) OR last_day = :prev" if prev_last else "attribute_not_exists(last_day)"
        vals: dict[str, Any] = {":xp": xp, ":streak": streak, ":day": last_day, ":ts": ts}
        if prev_last:
            vals[":prev"] = prev_last
        ops.append(update("PROFILE", "ADD xp_total :xp SET streak = :streak, last_day = :day, updated_at = :ts",
                          vals, condition=guard))

    day_adds, day_vals = [], {}
    if earned:
        day_adds.append("xp :xp")
        day_vals[":xp"] = xp
    if learned:
        day_adds.append("learned :learned")
        day_vals[":learned"] = learned
    if day_adds:
        ops.append(update(f"DAY#{day}", "ADD " + ", ".join(day_adds), day_vals))

    if rated_pass:
        # Best first-try score in percent (FR-004: the practice test shows its best score).
        score = round(100 * s.first_try_correct / s.unique_items) if s.unique_items else 0
        best_score = max(score, int(lesson_item.get("best_score", 0)))
        ops.append(update(f"LESSON#{s.lesson_id}",
                          "SET best_stars = :b, best_score = :bs, last_completed = :ts ADD completions :one",
                          {":b": stars, ":bs": best_score, ":ts": ts, ":one": 1}))

    # Mistakes: a first-try miss adds or bumps the item; a first-try hit clears it.
    touched: set[str] = set()
    for a in s.answers:
        if a.item_id == "match" or a.retry or a.item_id in touched:
            continue
        touched.add(a.item_id)
        sk = f"MISTAKE#{a.item_id}"
        if a.correct:
            ops.append({"Delete": {"TableName": table_name, "Key": {"PK": {"S": pk}, "SK": {"S": sk}}}})
        else:
            ops.append(update(sk, "ADD wrong_count :one SET last_wrong = :ts", {":one": 1, ":ts": ts}))
    ops.extend(put(row) for row in item_writes)

    try:
        client.transact_write_items(TransactItems=ops)
    except ClientError as err:
        if err.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = [r.get("Code") for r in err.response.get("CancellationReasons", [])]
            if reasons and reasons[0] == "ConditionalCheckFailed":
                again = client.get_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": f"SESSION#{s.session_id}"}})
                if "Item" in again:
                    item = {k: _de.deserialize(v) for k, v in again["Item"].items()}
                    raise DuplicateSession(_plain(item.get("result", {}))) from err
            raise ConcurrentUpdate() from err
        raise
    return result


class ConcurrentUpdate(Exception):
    pass


def _get_items(client: Any, table_name: str, pk: str, question_ids: list[str]) -> dict[str, dict]:
    """Current ITEM# rows for these questions (at most MAX_ANSWERS, so one batch of up to 100 keys)."""
    if not question_ids:
        return {}
    keys = [{"PK": {"S": pk}, "SK": {"S": f"ITEM#{q}"}} for q in question_ids]
    out: dict[str, dict] = {}
    request: dict[str, Any] = {table_name: {"Keys": keys, "ConsistentRead": True}}
    while request:
        resp = client.batch_get_item(RequestItems=request)
        for raw in resp.get("Responses", {}).get(table_name, []):
            item = {k: _de.deserialize(v) for k, v in raw.items()}
            out[item["SK"].split("#", 1)[1]] = item
        request = resp.get("UnprocessedKeys") or {}
    return out


def delete_user_data(sub: str) -> int:
    """Delete every item under the user's partition, and the link to their App Store subscription.
    Returns the number deleted."""
    table = _table()
    apple = table.get_item(Key={"PK": _pk(sub), "SK": "ENTITLEMENT#apple"}).get("Item")
    if apple and apple.get("original_transaction_id") and apple_owner(apple["original_transaction_id"]) == sub:
        table.delete_item(Key={"PK": _apple_pk(apple["original_transaction_id"]), "SK": "OWNER"})
    kwargs: dict[str, Any] = {"KeyConditionExpression": Key("PK").eq(_pk(sub)), "ProjectionExpression": "PK, SK"}
    deleted = 0
    with table.batch_writer() as batch:
        while True:
            resp = table.query(**kwargs)
            for item in resp.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                deleted += 1
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return deleted
