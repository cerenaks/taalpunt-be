#!/usr/bin/env bash
# Create (once) a monthly AWS cost budget that emails you at 80% actual and 100% forecasted spend.
# Usage: scripts/create_budget.sh you@example.com [limit-in-USD, default 5]
# Needs AWS credentials for the account (e.g. AWS_PROFILE=taalpunt).
set -euo pipefail

EMAIL="${1:?Usage: $0 you@example.com [limit-usd]}"
LIMIT="${2:-5}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

aws budgets create-budget \
  --account-id "$ACCOUNT" \
  --budget "{\"BudgetName\":\"taalpunt-monthly\",\"BudgetType\":\"COST\",\"TimeUnit\":\"MONTHLY\",\"BudgetLimit\":{\"Amount\":\"$LIMIT\",\"Unit\":\"USD\"}}" \
  --notifications-with-subscribers "[
    {\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":80,\"ThresholdType\":\"PERCENTAGE\"},
     \"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$EMAIL\"}]},
    {\"Notification\":{\"NotificationType\":\"FORECASTED\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},
     \"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$EMAIL\"}]}
  ]"

echo "Budget taalpunt-monthly created: $LIMIT USD/month, alerts to $EMAIL"
