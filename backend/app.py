#!/usr/bin/env python3
import os

import aws_cdk as cdk

from taalpunt.api_stack import TaalPuntStack
from taalpunt.github_deploy_stack import GithubDeployStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or "eu-north-1",
)

TaalPuntStack(app, "TaalPunt", env=env, description="TaalPunt KNM app backend")
GithubDeployStack(app, "TaalPuntGithubDeploy", env=env, description="GitHub Actions OIDC deploy role for TaalPunt")

cdk.Tags.of(app).add("project", "taalpunt")
app.synth()
