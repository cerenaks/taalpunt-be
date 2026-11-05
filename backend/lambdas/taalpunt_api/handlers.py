"""Lambda entry points behind the HTTP API. API Gateway has already verified the Cognito JWT."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from . import store
from .validation import ValidationError, parse_session

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

MAX_BODY_BYTES = 16 * 1024
DAILY_GOAL_XP = 50


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


def _json_body(event: dict) -> Any:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
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
            return _resp(200, progress)
        if route == "GET /me/mistakes":
            return _resp(200, {"mistakes": store.get_mistakes(sub)})
        if route == "POST /me/sessions":
            session = parse_session(_json_body(event))
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
