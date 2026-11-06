# TaalPunt

A KNM (Kennis van de Nederlandse Maatschappij) practice app: short lessons, hearts, XP, streaks and a "practice my mistakes" round, built from three KNM topics:

1. Wonen
2. Werk en inkomen
3. Interactie, normen en waarden

This repo (`taal-punt-be`) holds the lesson content and the AWS backend. The SwiftUI iOS app lives in its own folder, `taal-punt-ios`.

```
content/        knm.json (all questions + vocabulary) and its JSON schema
scripts/        validate_content.py, create_budget.sh, mock_server.py
backend/        AWS CDK app (Python) + Lambda code + tests
.github/        CI (tests, synth) and deploy (OIDC, no stored keys)
```

## Architecture

| Part | AWS service | Notes |
|---|---|---|
| Lesson content | S3 (private) + CloudFront | `content/manifest.json` (60 s cache) points at `content/knm-<version>.json` (immutable). The app downloads the bundle once and works offline. |
| Sign-in | Cognito user pool | Email, plus Sign in with Apple and Google once switched on in `cdk.json` (see below). Public client with PKCE, no client secret. |
| API | API Gateway HTTP API + Lambda (Python 3.12, arm64) | Every route needs a Cognito access token. Throttled at 10 req/s, burst 20. JSON access logs. |
| User data | DynamoDB (on-demand) | One partition per user. Point-in-time recovery, deletion protection, encryption at rest. Session history expires after a year. |
| Alerts | CloudWatch alarms + SNS email | Only created when `alert_email` is set. |
| Cost guard | AWS Budgets | Account-wide, created once with `scripts/create_budget.sh` (CloudFormation can't create budgets in eu-north-1). |
| Deploys | GitHub Actions + OIDC | The deploy role trusts only this repo's `production` environment and can only assume the CDK bootstrap roles. |

Region: **eu-north-1 (Stockholm)**.

## API

Base URL: the `ApiUrl` stack output. Send `Authorization: Bearer <Cognito access token>`. Optional header `x-local-date: YYYY-MM-DD` so streaks follow your calendar day.

### `GET /me/progress`

```json
{
  "xp_total": 120, "streak": 4, "last_day": "2026-11-05", "today_xp": 30, "daily_goal_xp": 50,
  "lessons": { "w1": { "best_stars": 2, "completions": 2, "last_completed": "2026-11-05T21:10:00+00:00" } },
  "days": [ { "date": "2026-11-05", "xp": 30 } ],
  "mistakes_count": 7
}
```

### `GET /me/mistakes`

`{ "mistakes": [ { "item_id": "k3-04", "wrong_count": 3, "last_wrong": "..." } ] }`, most-missed first. `item_id` is a question id (`w1-03`) or a word id (`wonen-v07`) from `knm.json`.

### `POST /me/sessions`

Send once per finished (or failed) lesson. Safe to retry: the same `session_id` returns the first result with `"duplicate": true`.

```json
{
  "session_id": "6f1c2c1e-6f86-4a3e-9a51-8c9a3a0d2b11",
  "kind": "lesson",
  "lesson_id": "w1",
  "local_date": "2026-11-05",
  "hearts_left": 4,
  "completed": true,
  "answers": [
    { "item_id": "w1-01", "correct": true },
    { "item_id": "w1-02", "correct": false },
    { "item_id": "w1-02", "correct": true, "retry": true },
    { "item_id": "match", "correct": true }
  ]
}
```

- `kind` / `lesson_id`: `lesson` + `w1` … · `vocab` + `voc-<unit>` · `test` + `test-<unit>` · `mix` + `mix` · `mistakes` + `mistakes`.
- `hearts_left` must equal `5 − wrong answers` (matching rounds don't cost hearts); the server rejects anything else.
- Response `201`: `{ "xp_gained": 9, "stars": 1, "streak": 1, "xp_total": 9, "duplicate": false }`.

Scoring (in `backend/lambdas/taalpunt_api/scoring.py`): 5 XP per finished lesson, +2 per first-try correct answer, +5 if no hearts were lost. Stars: one for every passed run of a lesson, vocabulary round or practice test, up to 3 (mix and mistakes rounds get none). `stars` in the response is the lesson's star count after this run. A first-try miss adds the item to your mistakes; a first-try hit removes it.

### `DELETE /me`

Deletes all your progress and your Cognito account (Apple requires in-app account deletion).

## Run the backend locally (no AWS needed)

`scripts/mock_server.py` serves the content and the API on `http://127.0.0.1:8787`, running the real Lambda code against an in-memory DynamoDB. Any bearer token is accepted. The iOS app's Debug build talks to it by default.

```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && cd ..
python scripts/mock_server.py
```

## First deploy (once, from your Mac)

You need Node 22, Python 3.12+, the AWS CLI v2, and an IAM Identity Center user with admin rights (not the root user).

```bash
# 1. Sign in to AWS
aws configure sso --profile taalpunt      # pick your account and the admin permission set, region eu-north-1
aws sso login --profile taalpunt

# 2. Install
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python ../scripts/validate_content.py && python -m pytest -q

# 3. Prepare the account for CDK and create the GitHub deploy role
export AWS_PROFILE=taalpunt
npx aws-cdk@2.1146.0 bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/eu-north-1
npx aws-cdk@2.1146.0 deploy TaalPuntGithubDeploy      # prints DeployRoleArn

# 4. Deploy the backend. The alert email stays out of this public repo: keep it in ~/.cdk.json
#    so every local deploy includes it. Without it, a deploy removes the alarms.
echo '{ "context": { "alert_email": "you@example.com" } }' > ~/.cdk.json
npx aws-cdk@2.1146.0 deploy TaalPunt

# 5. Cost alert: 5 USD/month budget, emails at 80% actual and 100% forecasted
cd .. && bash scripts/create_budget.sh you@example.com 5
```

Then confirm the "AWS Notification - Subscription Confirmation" email so alarms reach you.

**New AWS accounts:** CloudFront stays blocked until AWS Support verifies the account ("Your account must be verified before you can add new CloudFront resources"). Until then `content_cdn` is `false` in `backend/cdk.json`: the API serves the manifest and bundle at `GET /content/{file}` (public, read-only, straight from the private bucket), and `ContentManifestUrl` points at the API. After verification, set `content_cdn` to `true`, deploy, and put the new CloudFront host in the app's `CONTENT_HOST`.

The new **Free plan** also blocks creating IAM identity providers, so `TaalPuntGithubDeploy` (GitHub OIDC) only works after upgrading to the Paid plan. Until then, deploy from your Mac.

If step 4 fails because the Cognito domain prefix `taalpunt-app` is taken, change `cognito_domain_prefix` in `backend/cdk.json` and deploy again.

### Hand deploys over to GitHub

In the repo: **Settings → Environments → New environment** named `production`, then add two environment variables:

- `AWS_DEPLOY_ROLE_ARN` = the `DeployRoleArn` output from step 3
- `ALERT_EMAIL` = your email

From then on every push to `main` that touches `backend/` or `content/` runs tests and deploys. Add yourself as a required reviewer on the environment if you want to approve each deploy.

## Editing questions

1. Edit `content/knm.json`. Keep question ids stable (`<lesson>-NN`) so saved mistakes keep pointing at the right question.
2. Bump `content_version` (for example `2026-11-12.1`).
3. `python scripts/validate_content.py`
4. Push. The deploy uploads the new bundle and refreshes the manifest; the app picks it up on next launch.

## Turning on Sign in with Apple and Google

Both go through Cognito managed login, so the app never sees Apple or Google tokens and the API keeps checking the same Cognito JWT. Below, `<cognito-domain>` is `taalpunt-app.auth.eu-north-1.amazoncognito.com`.

**Apple** (needs the paid Apple Developer account, nothing extra to pay):

1. In Certificates, Identifiers & Profiles, open the app's App ID (`nl.taalpunt.app`) and turn on **Sign in with Apple**.
2. Create a **Services ID** (for example `nl.taalpunt.app.signin`). Turn on Sign in with Apple, then under Configure add the domain `<cognito-domain>` and the return URL `https://<cognito-domain>/oauth2/idpresponse`.
3. Create a **Key** with Sign in with Apple turned on and download the `.p8`. Write down the Key ID. Apple lets you download it only once.

**Google** (free):

1. In the [Google Cloud console](https://console.cloud.google.com), create a project called `TaalPunt`.
2. Set up the **OAuth consent screen**: External, app name TaalPunt, support email `taalpunt.support@gmail.com`, privacy policy `https://cerenaks.github.io/taalpunt-be/privacy.html`, scopes `openid`, `email` and `profile` only. Then **Publish app**. With only these scopes Google doesn't require a review. Don't upload a logo, because a logo starts Google's branding check.
3. Under Credentials, create an **OAuth client ID** of type **Web application** (Cognito is the web client here, not the iOS app). Add the authorized JavaScript origin `https://<cognito-domain>` and the redirect URI `https://<cognito-domain>/oauth2/idpresponse`. Write down the client ID and the client secret.

**Add the providers in the Cognito console** (free). The Apple key and the Google client secret are stored only inside Cognito, never in git, in CloudFormation or in Secrets Manager. Do this before switching them on in `cdk.json`, because the deploy fails if a provider doesn't exist yet.

1. Sign in to the AWS console in region **eu-north-1** and open Amazon Cognito, then the user pool, then **Authentication → Social and external providers → Add identity provider**.
2. **Sign in with Apple:**
   - Services ID: `nl.taalpunt.app.signin`
   - Team ID: `87Y8KSLP2V`
   - Key ID: `S4AUM7M7Y6`
   - Private key: open the `.p8` in TextEdit and paste the whole text, including the BEGIN and END lines.
   - Authorized scopes: `email`
   - Attribute mapping: user pool attribute `email` ← Apple attribute `email`.
3. **Google:**
   - Client ID: `268082522132-tj9c78cod7j0eha5l9kja5h42ukpgkcd.apps.googleusercontent.com`
   - Client secret: paste it from the Google console.
   - Authorized scopes: `openid email`
   - Attribute mapping: `email` ← `email` and `email_verified` ← `email_verified`.
4. Keep the `.p8` in your password manager, then delete it from Downloads.

If you ever rebuild the user pool, add the providers again. If you lose the key or the secret, create a new one in Apple's or Google's console and paste it into the same screen.

**Switch it on:**

1. In `backend/cdk.json`, set `apple_signin.enabled` and `google_signin.enabled` to `true`, then deploy. This only adds the two providers to the iOS app client.
2. In the iOS app's `Config/Backend.local.xcconfig`, set `SIGNIN_PROVIDERS = apple,google`. The sign-in screen then shows Apple, Google and email.

A person who signs in with Apple or Google gets a separate Cognito user from an email account with the same address. Progress isn't merged.
