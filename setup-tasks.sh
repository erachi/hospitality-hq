#!/bin/bash
# ============================================================
# Hospitality HQ — Seed the tasks S3 bucket with static config.
#
# Uploads seed/properties.json and seed/users.json to
# s3://<bucket>/config/ so the task Lambdas can read them.
#
# Usage:
#   ./setup-tasks.sh [bucket-name]
# Defaults to hospitality-hq-tasks.
#
# Before you run: edit seed/users.json and replace the
# REPLACE_WITH_*_SLACK_USER_ID placeholders with real Slack
# member IDs (find them via Slack profile → ⋮ → Copy member ID).
# ============================================================

set -e

BUCKET="${1:-hospitality-hq-tasks}"
REGION="${REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Seeding s3://${BUCKET}/config/ with properties and users..."

if grep -q "REPLACE_WITH" "${SCRIPT_DIR}/seed/users.json"; then
  echo ""
  echo "  WARNING: seed/users.json still has REPLACE_WITH placeholders."
  echo "  Edit the file and add real Slack user IDs, then re-run."
  exit 1
fi

aws s3 cp "${SCRIPT_DIR}/seed/properties.json" \
  "s3://${BUCKET}/config/properties.json" \
  --content-type application/json \
  --region "${REGION}"

aws s3 cp "${SCRIPT_DIR}/seed/users.json" \
  "s3://${BUCKET}/config/users.json" \
  --content-type application/json \
  --region "${REGION}"

echo ""
echo "Seed data uploaded. The task Lambdas will pick it up on next invocation."
echo "(Config is cached per cold start — deploy a new version or wait ~15min to refresh.)"
