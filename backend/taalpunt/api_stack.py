"""TaalPunt backend: content CDN, sign-in, API and user data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    SecretValue,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = ROOT / "backend" / "lambdas"
CONTENT_FILE = ROOT / "content" / "knm.json"


def content_bundle() -> tuple[str, str, dict]:
    """Returns (bundle key, bundle text, manifest) for the current content file."""
    text = CONTENT_FILE.read_text(encoding="utf-8")
    doc = json.loads(text)
    version = doc["content_version"]
    key = f"content/knm-{version}.json"
    manifest = {
        "schema_version": doc["schema_version"],
        "content_version": version,
        "path": key,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "bytes": len(text.encode("utf-8")),
        "questions": sum(len(l["questions"]) for u in doc["units"] for l in u["lessons"]),
        "words": sum(len(u["vocabulary"]) for u in doc["units"]),
    }
    return key, text, manifest


class TaalPuntStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        alert_email: str | None = ctx("alert_email") or None
        domain_prefix: str = ctx("cognito_domain_prefix") or "taalpunt-app"
        callback_urls: list[str] = ctx("callback_urls") or ["taalpunt://auth/callback"]
        logout_urls: list[str] = ctx("logout_urls") or ["taalpunt://auth/signout"]
        apple: dict = ctx("apple_signin") or {}
        apple_enabled = bool(apple.get("enabled"))

        # ---------- Content: private S3 bucket behind CloudFront ----------
        content_bucket = s3.Bucket(
            self, "ContentBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cdn = cloudfront.Distribution(
            self, "ContentCdn",
            comment="TaalPunt lesson content",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(content_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
        )
        bundle_key, bundle_text, manifest = content_bundle()
        # The bundle file name carries its version, so it can be cached forever.
        bundle_deploy = s3deploy.BucketDeployment(
            self, "DeployContentBundle",
            destination_bucket=content_bucket,
            sources=[s3deploy.Source.data(bundle_key, bundle_text)],
            prune=False,
            content_type="application/json; charset=utf-8",
            cache_control=[s3deploy.CacheControl.from_string("public, max-age=31536000, immutable")],
        )
        # The manifest is small and short-lived; the app checks it on launch.
        manifest_deploy = s3deploy.BucketDeployment(
            self, "DeployContentManifest",
            destination_bucket=content_bucket,
            sources=[s3deploy.Source.data("content/manifest.json", json.dumps(manifest, indent=2))],
            prune=False,
            content_type="application/json; charset=utf-8",
            cache_control=[s3deploy.CacheControl.from_string("public, max-age=60")],
            distribution=cdn,
            distribution_paths=["/content/manifest.json"],
        )
        manifest_deploy.node.add_dependency(bundle_deploy)

        # ---------- Sign-in: Cognito user pool (+ Sign in with Apple when configured) ----------
        user_pool = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)),
            password_policy=cognito.PasswordPolicy(min_length=12, require_symbols=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        domain = user_pool.add_domain("HostedDomain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix))

        providers = [cognito.UserPoolClientIdentityProvider.COGNITO]
        apple_idp = None
        if apple_enabled:
            apple_idp = cognito.UserPoolIdentityProviderApple(
                self, "AppleIdP",
                user_pool=user_pool,
                client_id=apple["services_id"],
                team_id=apple["team_id"],
                key_id=apple["key_id"],
                # The .p8 key text lives only in Secrets Manager; you put it there yourself.
                private_key_value=SecretValue.secrets_manager(apple.get("private_key_secret", "taalpunt/apple-signin-key")),
                scopes=["email", "name"],
                attribute_mapping=cognito.AttributeMapping(email=cognito.ProviderAttribute.APPLE_EMAIL),
            )
            providers.append(cognito.UserPoolClientIdentityProvider.APPLE)

        ios_client = user_pool.add_client(
            "IosClient",
            generate_secret=False,  # public mobile client; uses PKCE
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            supported_identity_providers=providers,
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )
        if apple_idp:
            ios_client.node.add_dependency(apple_idp)

        # ---------- User data: one DynamoDB table ----------
        table = dynamodb.Table(
            self, "UserData",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            time_to_live_attribute="expires_at",
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------- Functions ----------
        code = lambda_.Code.from_asset(str(LAMBDA_DIR), exclude=["**/__pycache__", "**/*.pyc"])

        def fn(name: str, handler: str, env: dict) -> lambda_.Function:
            log_group = logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_MONTH,
                                      removal_policy=RemovalPolicy.DESTROY)
            return lambda_.Function(
                self, name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler=handler,
                code=code,
                memory_size=256,
                timeout=Duration.seconds(10),
                environment={"TABLE_NAME": table.table_name, "LOG_LEVEL": "INFO", **env},
                tracing=lambda_.Tracing.ACTIVE,
                log_group=log_group,
            )

        api_fn = fn("ApiFunction", "taalpunt_api.handlers.api", {})
        delete_fn = fn("DeleteAccountFunction", "taalpunt_api.handlers.delete_account",
                       {"USER_POOL_ID": user_pool.user_pool_id})

        # Least privilege: only the item-level actions each function uses.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
                     "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"],
            resources=[table.table_arn],
        ))
        delete_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:Query", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"],
            resources=[table.table_arn],
        ))
        delete_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cognito-idp:AdminDeleteUser"],
            resources=[user_pool.user_pool_arn],
        ))

        # ---------- HTTP API with JWT auth on every route ----------
        jwt_auth = authorizers.HttpJwtAuthorizer(
            "CognitoJwt",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[ios_client.user_pool_client_id],
        )
        api = apigw.HttpApi(self, "Api", api_name="taalpunt-api", create_default_stage=False)
        stage = api.add_stage(
            "DefaultStage", stage_name="$default", auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        access_logs = logs.LogGroup(self, "ApiAccessLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY)
        cfn_stage: apigw.CfnStage = stage.node.default_child  # type: ignore[assignment]
        cfn_stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=json.dumps({
                "requestId": "$context.requestId", "ip": "$context.identity.sourceIp",
                "time": "$context.requestTime", "route": "$context.routeKey",
                "status": "$context.status", "latencyMs": "$context.responseLatency",
                "user": "$context.authorizer.claims.sub", "error": "$context.integrationErrorMessage",
            }),
        )
        api_int = integrations.HttpLambdaIntegration("ApiIntegration", api_fn)
        delete_int = integrations.HttpLambdaIntegration("DeleteIntegration", delete_fn)
        for path, method in (("/me/progress", apigw.HttpMethod.GET),
                             ("/me/mistakes", apigw.HttpMethod.GET),
                             ("/me/sessions", apigw.HttpMethod.POST)):
            api.add_routes(path=path, methods=[method], integration=api_int, authorizer=jwt_auth)
        api.add_routes(path="/me", methods=[apigw.HttpMethod.DELETE], integration=delete_int, authorizer=jwt_auth)

        # ---------- Alerts and cost guard ----------
        if alert_email:
            topic = sns.Topic(self, "Alerts", display_name="TaalPunt alerts", enforce_ssl=True)
            topic.add_subscription(subs.EmailSubscription(alert_email))
            alarms = [
                cw.Alarm(self, "ApiFunctionErrors", metric=api_fn.metric_errors(period=Duration.minutes(5)),
                         threshold=1, evaluation_periods=1,
                         treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                         alarm_description="The API function threw errors."),
                cw.Alarm(self, "Api5xx", metric=api.metric_server_error(period=Duration.minutes(5)),
                         threshold=5, evaluation_periods=1,
                         treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                         alarm_description="The API returned 5 or more server errors in 5 minutes."),
                cw.Alarm(self, "ApiThrottled", metric=api_fn.metric_throttles(period=Duration.minutes(5)),
                         threshold=1, evaluation_periods=1,
                         treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
                         alarm_description="The API function is being throttled."),
            ]
            for a in alarms:
                a.add_alarm_action(cw_actions.SnsAction(topic))
            # The monthly cost budget is account-wide and CloudFormation can't create it in
            # eu-north-1, so it is set up once with scripts/create_budget.sh instead.

        # ---------- Outputs the iOS app needs ----------
        CfnOutput(self, "Region", value=self.region)
        CfnOutput(self, "ApiUrl", value=api.api_endpoint)
        CfnOutput(self, "ContentManifestUrl", value=f"https://{cdn.distribution_domain_name}/content/manifest.json")
        CfnOutput(self, "ContentVersion", value=manifest["content_version"])
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=ios_client.user_pool_client_id)
        CfnOutput(self, "AuthDomain", value=domain.base_url())
