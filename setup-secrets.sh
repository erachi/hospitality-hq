#!/bin/bash
# ============================================================
# Hospitality HQ — Store secrets in AWS SSM Parameter Store
#
# Usage:
#   ./setup-secrets.sh <hospitable-token> <claude-api-key> <slack-bot-token> [webhook-signing-secret]
# ============================================================

set -e

PREFIX="/hospitality-hq"
REGION="us-east-1"

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    echo "Usage: ./setup-secrets.sh <hospitable-token> <claude-api-key> <slack-bot-token> [webhook-signing-secret]"
    exit 1
fi

TOTAL=3
if [ -n "$4" ]; then
    TOTAL=4
fi

echo "Storing secrets in SSM Parameter Store..."

aws ssm put-parameter \
  --name "${PREFIX}/hospitable-api-token" \
  --type SecureString \
  --value "$1" \
  --overwrite \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
echo "  1/${TOTAL} Hospitable token stored"

aws ssm put-parameter \
  --name "${PREFIX}/anthropic-api-key" \
  --type SecureString \
  --value "$2" \
  --overwrite \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
echo "  2/${TOTAL} Claude API key stored"

aws ssm put-parameter \
  --name "${PREFIX}/slack-bot-token" \
  --type SecureString \
  --value "$3" \
  --overwrite \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
echo "  3/${TOTAL} Slack bot token stored"

if [ -n "$4" ]; then
    aws ssm put-parameter \
      --name "${PREFIX}/webhook-signing-secret" \
      --type SecureString \
      --value "$4" \
      --overwrite \
      --region "${REGION}" \
      --no-cli-pager > /dev/null
    echo "  4/${TOTAL} Webhook signing secret stored"
fi

echo ""
echo "All secrets stored securely."
echo "Run: history -c && clear"
