"""Infrastructure assertions: the security properties we rely on stay in the template."""
import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from taalpunt.api_stack import TaalPuntStack
from taalpunt.github_deploy_stack import GithubDeployStack

ENV = cdk.Environment(account="111111111111", region="eu-north-1")


def synth(context=None):
    # Skip building the Lambda layer (pip downloads); the deploy builds it.
    app = cdk.App(context={"aws:cdk:bundling-stacks": [], **(context or {})})
    return Template.from_stack(TaalPuntStack(app, "T", env=ENV))


def _routes(t):
    return {r["Properties"]["RouteKey"]: r["Properties"].get("AuthorizationType", "NONE")
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()}


def test_every_route_requires_jwt_except_apples_notifications():
    routes = _routes(synth({"content_cdn": True}))
    # Apple can't sign in; every notification carries Apple's signature, which the function checks.
    assert routes.pop("POST /appstore/notifications") == "NONE"
    assert len(routes) == 8 and set(routes.values()) == {"JWT"}
    assert {"POST /me/purchases", "POST /me/saved", "POST /me/exam"} <= set(routes)


def test_without_cdn_only_the_content_route_is_public():
    t = synth({"content_cdn": "false"})
    t.resource_count_is("AWS::CloudFront::Distribution", 0)
    routes = _routes(t)
    assert routes.pop("GET /content/{file}") == "NONE"
    assert routes.pop("POST /appstore/notifications") == "NONE"
    assert len(routes) == 8 and set(routes.values()) == {"JWT"}
    assert "GET /me/content/premium" in routes


def test_with_cdn_there_is_one_distribution():
    synth({"content_cdn": True}).resource_count_is("AWS::CloudFront::Distribution", 1)


def test_table_is_protected():
    synth().has_resource("AWS::DynamoDB::Table", {
        "DeletionPolicy": "Retain",
        "Properties": Match.object_like({
            "DeletionProtectionEnabled": True,
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
            "SSESpecification": {"SSEEnabled": True},
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
        }),
    })


def test_content_bucket_is_private():
    synth().has_resource_properties("AWS::S3::Bucket", {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
        },
    })


def test_ios_client_has_no_secret_and_uses_code_flow():
    synth().has_resource_properties("AWS::Cognito::UserPoolClient", {
        "GenerateSecret": False,
        "AllowedOAuthFlows": ["code"],
        "CallbackURLs": ["taalpunt://auth/callback"],
    })


def test_api_function_cannot_scan_or_delete_users():
    t = synth()
    policies = t.find_resources("AWS::IAM::Policy")
    actions = []
    for p in policies.values():
        for s in p["Properties"]["PolicyDocument"]["Statement"]:
            a = s["Action"]
            actions.extend(a if isinstance(a, list) else [a])
    assert "dynamodb:Scan" not in actions
    assert "dynamodb:*" not in actions
    assert actions.count("cognito-idp:AdminDeleteUser") == 1
    # The API reads a session's question states with one BatchGetItem (store._get_items). Moto doesn't check IAM,
    # so this is the test that catches a missing permission before it fails in production.
    assert "dynamodb:BatchGetItem" in actions


def test_alerts_only_with_email():
    assert synth().find_resources("AWS::CloudWatch::Alarm") == {}
    t = synth({"alert_email": "me@example.com"})
    t.resource_count_is("AWS::CloudWatch::Alarm", 3)
    t.resource_count_is("AWS::SNS::Subscription", 1)
    # Budgets can't be created by CloudFormation in eu-north-1; scripts/create_budget.sh does it.
    t.resource_count_is("AWS::Budgets::Budget", 0)


def test_email_only_by_default():
    t = synth()
    t.has_resource_properties("AWS::Cognito::UserPoolClient", {"SupportedIdentityProviders": ["COGNITO"]})


def test_social_providers_are_switched_on_without_secrets_in_the_template():
    t = synth({"apple_signin": {"enabled": True}, "google_signin": {"enabled": True}})
    t.has_resource_properties("AWS::Cognito::UserPoolClient",
                              {"SupportedIdentityProviders": ["COGNITO", "SignInWithApple", "Google"]})
    # The providers (and their secrets) are created in the Cognito console, never by CloudFormation.
    t.resource_count_is("AWS::Cognito::UserPoolIdentityProvider", 0)
    assert "secretsmanager" not in json.dumps(t.to_json())


def test_github_role_is_scoped_to_repo_environment():
    app = cdk.App()
    t = Template.from_stack(GithubDeployStack(app, "G", env=ENV))
    t.has_resource_properties("AWS::IAM::Role", {
        "AssumeRolePolicyDocument": {"Statement": [Match.object_like({
            "Condition": {"StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": "repo:cerenaks/taalpunt-be:environment:production",
            }},
        })]},
    })


# ---------- Free and Premium (FR-001) ----------

def test_premium_bundle_is_only_readable_by_the_api():
    t = synth({"content_cdn": "false"})
    policies = json.dumps(t.find_resources("AWS::IAM::Policy"))
    # The public content function can only read content/*, never premium/*.
    content_policy = [p for k, p in t.find_resources("AWS::IAM::Policy").items() if k.startswith("ContentFunction")]
    assert content_policy and "premium/*" not in json.dumps(content_policy)
    assert "premium/*" in policies


def test_premium_and_cdn_together_are_refused():
    import pytest
    with pytest.raises(ValueError, match="premium/ public"):
        synth({"content_cdn": True, "premium_enabled": True})


def test_premium_switch_reaches_the_api():
    off = synth({"content_cdn": "false"})
    off.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "taalpunt_api.handlers.api",
        "Environment": {"Variables": Match.object_like({"PREMIUM_ENABLED": "false"})}})
    on = synth({"content_cdn": "false", "premium_enabled": True})
    on.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "taalpunt_api.handlers.api",
        "Environment": {"Variables": Match.object_like({"PREMIUM_ENABLED": "true"})}})


def test_free_bundle_when_premium_is_on():
    from taalpunt.api_stack import content_bundle
    off, on = content_bundle(False), content_bundle(True)
    assert off["manifest"]["path"].endswith(".json") and "premium" not in off["manifest"]
    assert off["manifest"]["questions"] == 609
    assert on["manifest"]["path"].endswith("-free.json")
    assert on["manifest"]["questions"] == 140
    assert on["manifest"]["premium"]["path"] == "/me/content/premium"
    assert on["full_key"].startswith("premium/") and not on["public_key"].startswith("premium/")
    free = json.loads(on["public_text"])
    # Every theme and lesson is still listed, so the app can show locked stops.
    assert len(free["units"]) == 8
    assert all(l["questions"] for l in free["units"][0]["lessons"])


def test_purchase_functions_get_apples_library_and_app_ids():
    t = synth({"app_store": {"bundle_id": "nl.taalpunt.app", "app_apple_id": "1234567890"}})
    assert len(t.find_resources("AWS::Lambda::LayerVersion", {"Properties": {
        "Description": "App Store Server Library for checking purchases",
        "CompatibleArchitectures": ["arm64"], "CompatibleRuntimes": ["python3.12"]}})) == 1
    for handler in ("taalpunt_api.handlers.api", "taalpunt_api.handlers.appstore_notifications"):
        t.has_resource_properties("AWS::Lambda::Function", {
            "Handler": handler, "Layers": Match.any_value(),
            "Environment": {"Variables": Match.object_like({"BUNDLE_ID": "nl.taalpunt.app",
                                                            "APP_APPLE_ID": "1234567890"})}})


def test_notifications_function_only_reads_and_writes_items():
    t = synth()
    policies = [p for k, p in t.find_resources("AWS::IAM::Policy").items() if k.startswith("AppStoreNotifications")]
    actions = json.dumps(policies)
    assert "dynamodb:GetItem" in actions and "dynamodb:PutItem" in actions
    for forbidden in ("dynamodb:Query", "dynamodb:DeleteItem", "s3:", "cognito-idp:"):
        assert forbidden not in actions
