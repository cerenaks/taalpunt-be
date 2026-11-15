"""scripts/premium.py against moto: find learners by email and give or take Premium."""
import importlib.util
from pathlib import Path

import boto3
from moto import mock_aws

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "premium.py"
spec = importlib.util.spec_from_file_location("premium_script", SCRIPT)
premium = importlib.util.module_from_spec(spec)
spec.loader.exec_module(premium)


def test_finds_every_account_with_an_email_and_grants_premium(table, monkeypatch):
    with mock_aws():
        cognito = boto3.client("cognito-idp", region_name="eu-north-1")
        pool = cognito.create_user_pool(PoolName="p")["UserPool"]["Id"]
        for name in ("someone@example.com", "Google_123"):
            cognito.admin_create_user(UserPoolId=pool, Username=name,
                                      UserAttributes=[{"Name": "email", "Value": "someone@example.com"}])
        cognito.admin_create_user(UserPoolId=pool, Username="other@example.com",
                                  UserAttributes=[{"Name": "email", "Value": "other@example.com"}])
        accounts = premium.users_by_email(cognito, pool, " Someone@Example.com ")
        assert sorted(a["provider"] for a in accounts) == ["Google", "email"]

        from taalpunt_api import store
        for acc in accounts:
            store.grant_premium(acc["sub"], "manual", until=premium.until_iso("2027-06-30"))
        assert store.get_premium(accounts[0]["sub"]) == {
            "active": True, "until": "2027-06-30T23:59:59+00:00", "sources": ["manual"]}
        assert premium.email_for_sub(cognito, pool, accounts[0]["sub"]) == "someone@example.com"


def test_until_is_the_end_of_that_day():
    assert premium.until_iso("2027-06-30") == "2027-06-30T23:59:59+00:00"
    assert premium.until_iso(None) is None
