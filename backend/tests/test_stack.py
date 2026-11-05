"""Infrastructure assertions: the security properties we rely on stay in the template."""
import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from taalpunt.api_stack import TaalPuntStack
from taalpunt.github_deploy_stack import GithubDeployStack

ENV = cdk.Environment(account="111111111111", region="eu-north-1")


def synth(context=None):
    app = cdk.App(context=context or {})
    return Template.from_stack(TaalPuntStack(app, "T", env=ENV))


def test_every_route_requires_jwt():
    t = synth()
    routes = t.find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 4
    for r in routes.values():
        assert r["Properties"]["AuthorizationType"] == "JWT"


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


def test_alerts_only_with_email():
    assert synth().find_resources("AWS::CloudWatch::Alarm") == {}
    t = synth({"alert_email": "me@example.com"})
    t.resource_count_is("AWS::CloudWatch::Alarm", 3)
    t.resource_count_is("AWS::SNS::Subscription", 1)
    # Budgets can't be created by CloudFormation in eu-north-1; scripts/create_budget.sh does it.
    t.resource_count_is("AWS::Budgets::Budget", 0)


def test_apple_sign_in_is_optional():
    t = synth({"apple_signin": {"enabled": True, "services_id": "nl.example.taalpunt.signin",
                                 "team_id": "ABCDE12345", "key_id": "KEY1234567"}})
    t.resource_count_is("AWS::Cognito::UserPoolIdentityProvider", 1)
    t.has_resource_properties("AWS::Cognito::UserPoolClient", {"SupportedIdentityProviders": ["COGNITO", "SignInWithApple"]})


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
