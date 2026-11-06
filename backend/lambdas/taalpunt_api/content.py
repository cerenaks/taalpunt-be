"""GET /content/{file}: serves the lesson manifest and bundle from the private content bucket.

Used when the CloudFront CDN is turned off (context `content_cdn: false`), for example while a new
AWS account is still waiting for CloudFront verification. Public on purpose: the app needs the
questions before anyone signs in, the same as with the CDN.
"""
from __future__ import annotations

import os
import re
from typing import Any

ALLOWED = re.compile(r"^(manifest|knm-[0-9A-Za-z.\-]{1,40})\.json$")

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def _response(status: int, body: str, cache: str = "no-store") -> dict:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": cache,
            "x-content-type-options": "nosniff",
        },
        "body": body,
    }


def handler(event: dict, _context: Any) -> dict:
    name = (event.get("pathParameters") or {}).get("file", "")
    if not ALLOWED.match(name):
        return _response(404, '{"error":"not_found"}')
    try:
        obj = _client().get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=f"content/{name}")
    except Exception as exc:  # NoSuchKey and access errors look the same to the app
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return _response(404, '{"error":"not_found"}')
        raise
    body = obj["Body"].read().decode("utf-8")
    return _response(200, body, obj.get("CacheControl") or "public, max-age=60")
