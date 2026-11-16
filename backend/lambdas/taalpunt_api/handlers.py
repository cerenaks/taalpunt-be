"""Lambda entry points behind the HTTP API. API Gateway has already verified the Cognito JWT."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from . import store, tiers
from .validation import SAVABLE_ID, ValidationError, parse_session

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

MAX_BODY_BYTES = 16 * 1024
DAILY_GOAL_XP = 50  # still sent for app versions before FR-002
# FR-002: the daily goal counts questions learned (moved up to learning or known) that day.
DAILY_GOAL_QUESTIONS = 10


def _premium_enabled() -> bool:
    """FR-001 switch (cdk.json premium_enabled). Off: everything works as before Premium."""
    return os.environ.get("PREMIUM_ENABLED", "false") == "true"


def _premium_sets() -> tuple[set[str], set[str]]:
    lessons = {x for x in os.environ.get("PREMIUM_LESSONS", "").split(",") if x}
    units = {x for x in os.environ.get("PREMIUM_UNITS", "").split(",") if x}
    return lessons, units


def _premium_bundle() -> str:
    import boto3
    obj = boto3.client("s3").get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=os.environ["PREMIUM_BUNDLE_KEY"])
    return obj["Body"].read().decode("utf-8")


def _resp(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _error(status: int, code: str, message: str) -> dict:
    return _resp(status, {"error": code, "message": message})


def _sub(event: dict) -> str | None:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    sub = claims.get("sub")
    # Access tokens from the iOS client carry token_use=access; reject ID tokens used as bearer tokens.
    if not sub or claims.get("token_use") not in (None, "access"):
        return None
    return sub


def _local_today(event: dict) -> date:
    """Client sends its calendar date in a header so streaks follow the user's day, not UTC."""
    raw = (event.get("headers") or {}).get("x-local-date")
    utc_today = datetime.now(timezone.utc).date()
    if raw:
        try:
            d = date.fromisoformat(raw)
            if abs((d - utc_today).days) <= 1:
                return d
        except ValueError:
            pass
    return utc_today


def _json_body(event: dict, max_bytes: int = MAX_BODY_BYTES) -> Any:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")
    if len(body.encode("utf-8")) > max_bytes:
        raise ValidationError("Request body is too large.")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationError("Body must be valid JSON.") from exc


def api(event: dict, _context: Any) -> dict:
    sub = _sub(event)
    if not sub:
        return _error(401, "unauthorized", "Sign in again.")
    route = event.get("routeKey", "")
    try:
        if route == "GET /me/progress":
            progress = store.get_progress(sub, _local_today(event))
            progress["daily_goal_xp"] = DAILY_GOAL_XP
            progress["daily_goal_questions"] = DAILY_GOAL_QUESTIONS
            return _resp(200, progress)
        if route == "GET /me/mistakes":
            return _resp(200, {"mistakes": store.get_mistakes(sub)})
        if route == "GET /me/content/premium":
            if not store.get_premium(sub)["active"]:
                return _error(403, "premium_required", "This content is part of TaalPunt Premium.")
            return {"statusCode": 200,
                    "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "private, no-store"},
                    "body": _premium_bundle()}
        if route == "POST /me/purchases":
            from . import appstore
            try:
                # A signed transaction with its three certificates is about 8 KB.
                return _resp(200, {"premium": appstore.claim(sub, _json_body(event, 32 * 1024))})
            except appstore.PurchaseError as err:
                return _error(err.status, err.code, err.message)
        if route == "POST /me/exam":
            # FR-004: {"exam_date": "2026-12-01"} or {"exam_date": null}, or
            # {"exam_date": "2026-11-12", "outcome": "passed" | "retake" | "skipped"} once the date has passed.
            body = _json_body(event)
            if not isinstance(body, dict) or "exam_date" not in body:
                raise ValidationError('Body must have "exam_date" (YYYY-MM-DD or null).')
            raw, outcome = body.get("exam_date"), body.get("outcome")
            exam_day = None
            if raw is not None:
                try:
                    exam_day = date.fromisoformat(raw) if isinstance(raw, str) else None
                except ValueError:
                    exam_day = None
                if exam_day is None or not (date(2020, 1, 1) <= exam_day <= date(2100, 1, 1)):
                    raise ValidationError("exam_date must be YYYY-MM-DD.")
            if outcome is None:
                store.set_exam_date(sub, exam_day.isoformat() if exam_day else None)
            else:
                if outcome not in store.EXAM_OUTCOMES or exam_day is None:
                    raise ValidationError('outcome must be "passed", "retake" or "skipped", with the exam\'s date.')
                store.report_exam(sub, exam_day.isoformat(), outcome)
            return _resp(200, {"exam": store.get_progress(sub, _local_today(event))["exam"]})
        if route == "POST /me/saved":
            body = _json_body(event)
            item_id = body.get("item_id") if isinstance(body, dict) else None
            saved = body.get("saved") if isinstance(body, dict) else None
            if not isinstance(item_id, str) or not SAVABLE_ID.match(item_id) or not isinstance(saved, bool):
                raise ValidationError('Body must be {"item_id": "<question or word id>", "saved": true|false}.')
            store.set_saved(sub, item_id, saved)
            return _resp(200, {"item_id": item_id, "saved": saved})
        if route == "POST /me/sessions":
            session = parse_session(_json_body(event))
            if session.kind == "exam" and _premium_enabled() and not store.get_premium(sub)["active"]:
                # FR-004: exam practice is Premium only.
                return _error(403, "premium_required", "Exam practice is part of TaalPunt Premium.")
            if _premium_enabled():
                lessons, units = _premium_sets()
                touches = tiers.needs_premium(session.lesson_id, [a.item_id for a in session.answers], lessons, units)
                if touches and not store.get_premium(sub)["active"]:
                    return _error(403, "premium_required", "This lesson is part of TaalPunt Premium.")
            try:
                result = store.record_session(sub, session)
                return _resp(201, {**result, "duplicate": False})
            except store.DuplicateSession as dup:
                return _resp(200, {**dup.summary, "duplicate": True})
            except store.ConcurrentUpdate:
                return _error(409, "conflict", "Progress changed at the same time. Send the session again.")
        return _error(404, "not_found", "Unknown route.")
    except ValidationError as err:
        return _error(400, "invalid_request", str(err))
    except Exception:  # noqa: BLE001 - never leak internals to the client
        log.exception("unhandled error on %s", route)
        return _error(500, "internal", "Something went wrong. Try again later.")


# Apple's notifications carry three signed objects with their certificates.
MAX_NOTIFICATION_BYTES = 128 * 1024


def appstore_notifications(event: dict, _context: Any) -> dict:
    """POST /appstore/notifications: App Store Server Notifications V2. Public; trust comes from Apple's
    signature, not from the caller. Anything but 200 makes Apple send the notification again later."""
    from . import appstore

    try:
        result = appstore.notification(_json_body(event, MAX_NOTIFICATION_BYTES))
        log.info("App Store notification: %s", result)
        return _resp(200, {"ok": True})
    except ValidationError as err:
        return _error(400, "invalid_request", str(err))
    except appstore.PurchaseError as err:
        log.warning("App Store notification refused: %s", err.code)
        return _error(err.status, err.code, err.message)
    except Exception:  # noqa: BLE001
        log.exception("App Store notification failed")
        return _error(500, "internal", "Something went wrong.")


def delete_account(event: dict, _context: Any) -> dict:
    """DELETE /me: remove all stored progress, then the Cognito user. Required for App Store accounts."""
    sub = _sub(event)
    if not sub:
        return _error(401, "unauthorized", "Sign in again.")
    import boto3

    try:
        deleted = store.delete_user_data(sub)
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        username = claims.get("username") or claims.get("cognito:username") or sub
        boto3.client("cognito-idp").admin_delete_user(UserPoolId=os.environ["USER_POOL_ID"], Username=username)
        log.info("deleted account sub=%s items=%d", sub, deleted)
        return _resp(200, {"deleted": True})
    except Exception:  # noqa: BLE001
        log.exception("account deletion failed")
        return _error(500, "internal", "Your account could not be deleted. Try again later.")
