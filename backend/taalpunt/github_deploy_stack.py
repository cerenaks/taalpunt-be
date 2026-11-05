"""One-time stack: lets GitHub Actions deploy without stored AWS keys (OIDC).

Deploy this once by hand. It creates a role that only workflows in the given repo,
running in the `production` environment, can assume. The role can do nothing except
assume the CDK bootstrap roles, which do the actual deployment.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack, aws_iam as iam
from constructs import Construct

GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"


class GithubDeployStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        repo: str = ctx("github_repo") or "cerenaks/TaalPunt"
        environment: str = ctx("github_environment") or "production"
        existing_provider_arn: str | None = ctx("github_oidc_provider_arn") or None

        if existing_provider_arn:
            provider_arn = existing_provider_arn
        else:
            provider = iam.CfnOIDCProvider(
                self, "GithubOidc",
                url=f"https://{GITHUB_OIDC_HOST}",
                client_id_list=["sts.amazonaws.com"],
            )
            provider_arn = provider.attr_arn

        role = iam.Role(
            self, "GithubDeployRole",
            role_name="taalpunt-github-deploy",
            description=f"Assumed by GitHub Actions in {repo} ({environment}) to run cdk deploy.",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(provider_arn, conditions={
                "StringEquals": {
                    f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                    f"{GITHUB_OIDC_HOST}:sub": f"repo:{repo}:environment:{environment}",
                },
            }),
        )
        role.add_to_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-*-{self.account}-{self.region}"],
        ))

        CfnOutput(self, "DeployRoleArn", value=role.role_arn,
                  description="Save this as the AWS_DEPLOY_ROLE_ARN variable in the GitHub 'production' environment.")
