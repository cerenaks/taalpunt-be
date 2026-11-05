#!/usr/bin/env python3
"""Local stand-in for the AWS backend, for running the iOS app in the simulator.

Serves the same URLs the app uses in production:
  GET    /content/manifest.json          (CloudFront in production)
  GET    /content/knm-<version>.json
  GET    /me/progress                    (API Gateway + Lambda in production)
  GET    /me/mistakes
  POST   /me/sessions
  DELETE /me

The API routes run the real Lambda handler code against an in-memory DynamoDB (moto),
so scoring, streaks, mistakes and validation behave exactly like the deployed backend.
Any `Authorization: Bearer <anything>` is accepted; data is lost when you stop the server.

Usage (from taal-punt-be):
  source backend/.venv/bin/activate      # needs requirements-dev.txt
  python scripts/mock_server.py          # http://127.0.0.1:8787
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "lambdas"))

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-north-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "mock")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "mock")
os.environ["TABLE_NAME"] = "taalpunt-local"

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

API_ROUTES = {("GET", "/me/progress"), ("GET", "/me/mistakes"), ("POST", "/me/sessions"), ("DELETE", "/me")}


def load_content() -> tuple[str, bytes, bytes]:
    text = (ROOT / "content" / "knm.json").read_bytes()
    doc = json.loads(text)
    path = f"content/knm-{doc['content_version']}.json"
    manifest = {
        "schema_version": doc["schema_version"],
        "content_version": doc["content_version"],
        "path": path,
        "sha256": hashlib.sha256(text).hexdigest(),
        "bytes": len(text),
        "questions": sum(len(l["questions"]) for u in doc["units"] for l in u["lessons"]),
        "words": sum(len(u["vocabulary"]) for u in doc["units"]),
    }
    return "/" + path, text, json.dumps(manifest, indent=2).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "TaalPuntMock/1"
    bundle_path = ""
    bundle = b""
    manifest = b""

    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self) -> None:
        path = self.path.split("?", 1)[0]
        if self.command in ("GET", "HEAD") and path == "/content/manifest.json":
            return self._send(200, self.manifest, extra={"Cache-Control": "public, max-age=60"})
        if self.command in ("GET", "HEAD") and path == self.bundle_path:
            return self._send(200, self.bundle, extra={"Cache-Control": "public, max-age=31536000, immutable"})
        if (self.command, path) not in API_ROUTES:
            return self._send(404, b'{"message":"Not Found"}')

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) <= 7:
            # Same shape API Gateway returns when the JWT is missing.
            return self._send(401, b'{"message":"Unauthorized"}')

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else None
        sub = self.headers.get("X-Mock-User", "local-user")
        event = {
            "routeKey": f"{self.command} {path}",
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
            "isBase64Encoded": False,
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub, "token_use": "access", "username": sub}}}},
        }
        from taalpunt_api import handlers, store

        if self.command == "DELETE":
            store.delete_user_data(sub)  # no Cognito locally; just clear the data
            result = {"statusCode": 200, "body": json.dumps({"deleted": True})}
        else:
            result = handlers.api(event, None)
        self._send(result["statusCode"], result["body"].encode("utf-8"))

    do_GET = do_POST = do_DELETE = do_HEAD = _route

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"  {self.command:6} {self.path} -> {args[1] if len(args) > 1 else ''}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    Handler.bundle_path, Handler.bundle, Handler.manifest = load_content()
    with mock_aws():
        boto3.resource("dynamodb").create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        server = HTTPServer((args.host, args.port), Handler)
        print(f"TaalPunt mock backend on http://{args.host}:{args.port}")
        print(f"  content: http://{args.host}:{args.port}/content/manifest.json")
        print("  api:     GET /me/progress, GET /me/mistakes, POST /me/sessions, DELETE /me")
        print("Stop with Ctrl+C. Data is kept in memory only.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
