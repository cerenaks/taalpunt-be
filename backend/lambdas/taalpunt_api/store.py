"""DynamoDB access. One table, partitioned per user.

Key layout (PK = USER#<cognito sub>):
  SK = PROFILE              xp_total, streak, last_day
  SK = LESSON#<lesson_id>   best_stars, completions, last_completed
  SK = DAY#<yyyy-mm-dd>     xp
  SK = MISTAKE#<item_id>    wrong_count, last_wrong
  SK = SESSION#<uuid>       summary of one finished or failed lesson (expires after a year)
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
        }
        for i in _query_prefix(sub, "LESSON#")
    }
    days = [{"date": i["SK"].split("#", 1)[1], "xp": i.get("xp", 0)}
            for i in _query_prefix(sub, "DAY#", newest_first=True, limit=DAY_HISTORY)]
    today_xp = next((d["xp"] for d in days if d["date"] == today.isoformat()), 0)
    mistakes = _query_prefix(sub, "MISTAKE#")
    return _plain({
        "xp_total": profile.get("xp_total", 0),
        "streak": visible_streak(int(profile.get("streak", 0)), last_day, today),
        "last_day": profile.get("last_day"),
        "today_xp": today_xp,
        "lessons": lessons,
        "days": days,
        "mistakes_count": len(mistakes),
    })


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

    result = {"xp_gained": xp, "stars": stars, "streak": streak, "xp_total": xp_total}

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
        ops.append(update(f"DAY#{s.local_date.isoformat()}", "ADD xp :xp", {":xp": xp}))

    if rated_pass:
        ops.append(update(f"LESSON#{s.lesson_id}", "SET best_stars = :b, last_completed = :ts ADD completions :one",
                          {":b": stars, ":ts": ts, ":one": 1}))

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


def delete_user_data(sub: str) -> int:
    """Delete every item under the user's partition. Returns the number deleted."""
    table = _table()
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
