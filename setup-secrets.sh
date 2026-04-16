#!/bin/bash
# ============================================================
# Hospitality HQ — Store secrets in AWS SSM Parameter Store
#
# Usage:
#   ./setup-secrets.sh <hospitable-token> <claude-api-key> <slack-bot-token>
# ============================================================

set -e

PREFIX="/hospitality-hq"
REGION="us-east-1"

if [ "$#" -ne 3 ]; then
    echo "Usage: ./setup-secrets.sh <hospitable-token> <claude-api-key> <slack-bot-token>"
    exit 1
fi

echo "Storing secrets in SSM Parameter Store..."

aws ssm put-parameter \
  --name "${PREFIX}/hospitable-api-token" \
  --type SecureString \
  --value "$1" \
  --overwrite \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
echo "  1/3 Hospitable token stored"

aws ssm put-parameter \
  --name "${PREFIX}/anthropic-api-key" \
  --type SecureString \
  --value "$2" \
  --overwrite \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
echo "  2/3 Claude API key stored"

aws ssm put-parameter \
  --name "${PREFIX}/slack-bot-token" \
  --type SecureString \
  --value "$3" \
  --overwrite \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
echo "  3/3 Slack bot token stored"

echo ""
echo "All secrets stored securely."
echo "Run: history -c && clear"
