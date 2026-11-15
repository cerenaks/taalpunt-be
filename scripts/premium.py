#!/usr/bin/env python3
"""Give, take away or list Premium by hand (FR-001), for you, testers, partners or a school pilot.

Runs with your own AWS login (`aws login`); the app has no admin screen. Finds the learner in Cognito by
email and writes a "manual" Premium entitlement to DynamoDB. The learner gets Premium on their next sync.
A person can have more than one account with the same email (for example Google and email sign-in);
all of them get the change.

Usage (from taal-punt-be, with backend/.venv active):
  python scripts/premium.py grant  --email someone@example.com                      # no end date
  python scripts/premium.py grant  --email someone@example.com --until 2027-06-30   # until that day (end of day, UTC)
  python scripts/premium.py grant  --email someone@example.com --note "school pilot"
  python scripts/premium.py revoke --email someone@example.com
  python scripts/premium.py list
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "lambdas"))

STACK = "TaalPunt"
REGION = "eu-north-1"


def stack_names(cfn) -> tuple[str, str]:
    """(user pool id, table name) of the deployed stack."""
    outputs = {o["OutputKey"]: o["OutputValue"]
               for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])}
    tables = [r["PhysicalResourceId"] for r in cfn.describe_stack_resources(StackName=STACK)["StackResources"]
              if r["ResourceType"] == "AWS::DynamoDB::Table"]
    if "UserPoolId" not in outputs or len(tables) != 1:
        sys.exit("Couldn't find the user pool and table of the TaalPunt stack.")
    return outputs["UserPoolId"], tables[0]


def users_by_email(cognito, pool_id: str, email: str) -> list[dict]:
    """[{sub, username, email, provider}] for every account with this email."""
    users = cognito.list_users(UserPoolId=pool_id, Filter=f'email = "{email.strip().lower()}"')["Users"]
    out = []
    for u in users:
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        provider = u["Username"].split("_", 1)[0] if "_" in u["Username"] else "email"
        out.append({"sub": attrs.get("sub"), "username": u["Username"], "email": attrs.get("email"), "provider": provider})
    return out


def email_for_sub(cognito, pool_id: str, sub: str) -> str:
    users = cognito.list_users(UserPoolId=pool_id, Filter=f'sub = "{sub}"')["Users"]
    if not users:
        return "(account deleted)"
    attrs = {a["Name"]: a["Value"] for a in users[0].get("Attributes", [])}
    return attrs.get("email", "?")


def until_iso(day: str | None) -> str | None:
    if not day:
        return None
    d = date.fromisoformat(day)
    return f"{d.isoformat()}T23:59:59+00:00"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual Premium for TaalPunt learners.")
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("grant", help="give Premium")
    g.add_argument("--email", required=True)
    g.add_argument("--until", help="last day of Premium, YYYY-MM-DD (default: no end)")
    g.add_argument("--note", default="", help="why, for your own records")
    r = sub.add_parser("revoke", help="take manual Premium away (Apple subscriptions are not touched)")
    r.add_argument("--email", required=True)
    sub.add_parser("list", help="everyone with manual Premium")
    args = parser.parse_args(argv)

    session = boto3.Session(region_name=REGION)
    pool_id, table_name = stack_names(session.client("cloudformation"))
    os.environ["TABLE_NAME"] = table_name
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    from taalpunt_api import store  # needs TABLE_NAME

    cognito = session.client("cognito-idp")

    if args.command == "list":
        table = session.resource("dynamodb").Table(table_name)
        items, kwargs = [], {"FilterExpression": "SK = :s", "ExpressionAttributeValues": {":s": "ENTITLEMENT#manual"}}
        while True:
            resp = table.scan(**kwargs)
            items += resp.get("Items", [])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        if not items:
            print("Nobody has manual Premium.")
        for it in sorted(items, key=lambda i: i.get("granted_at", "")):
            learner = it["PK"].split("#", 1)[1]
            print(f"{email_for_sub(cognito, pool_id, learner):40} until {it.get('until', 'no end'):27} "
                  f"since {it.get('granted_at', '?')[:10]}  {it.get('note', '')}")
        return 0

    accounts = users_by_email(cognito, pool_id, args.email)
    if not accounts:
        print(f"No TaalPunt account with the email {args.email}. They need to sign in to the app once first.")
        return 1
    for acc in accounts:
        if args.command == "grant":
            store.grant_premium(acc["sub"], "manual", until=until_iso(args.until), note=args.note)
            print(f"Premium given to {acc['email']} ({acc['provider']} account) until {args.until or 'no end'}.")
        else:
            store.revoke_premium(acc["sub"], "manual")
            print(f"Manual Premium taken away from {acc['email']} ({acc['provider']} account).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
