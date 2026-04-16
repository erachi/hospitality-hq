# Hospitality HQ — Deployment Guide

## Prerequisites

1. **AWS CLI** installed and configured (`aws configure`)
2. **AWS SAM CLI** installed ([install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html))
3. **Python 3.12** installed locally
4. Your API credentials ready:
   - Hospitable API token (from Hospitable dashboard → Settings → API)
   - Claude API key (from [console.anthropic.com](https://console.anthropic.com))
   - Slack bot token (from your Slack app — see Slack Setup below)

## Step 1: Slack Setup

You need a Slack bot token to post messages programmatically (separate from the MCP connector you use in Claude).

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app
2. Choose "From scratch", name it "Hospitality HQ Bot", select your Hospitality HQ workspace
3. Go to **OAuth & Permissions** and add these Bot Token Scopes:
   - `chat:write` — post messages
   - `channels:read` — find channel IDs
4. Click **Install to Workspace** and authorize
5. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
6. In Slack, create a channel called `#guest-alerts`
7. Invite the bot to the channel: `/invite @Hospitality HQ Bot`
8. Get the channel ID: right-click the channel name → "View channel details" → the ID is at the bottom

## Step 2: Build & Deploy

From the `hospitality_hq` directory:

```bash
# Build the Lambda package
sam build

# Deploy (first time — guided setup)
sam deploy --guided
```

During guided deploy, you'll be prompted for:

| Parameter | What to enter |
|-----------|---------------|
| Stack name | `hospitality-hq` |
| AWS Region | `us-west-2` (or your preferred region) |
| HospitableApiToken | Your Hospitable API token |
| AnthropicApiKey | Your Claude API key |
| SlackBotToken | The `xoxb-` bot token from Step 1 |
| SlackChannelId | The channel ID from Step 1 |
| PropertyUuids | Leave default (your two properties are pre-configured) |
| ScheduleRate | Leave default (`rate(15 minutes)`) |

SAM will save your choices to `samconfig.toml` for future deploys.

## Step 3: Verify

```bash
# Invoke the function manually to test
sam remote invoke MonitorFunction --stack-name hospitality-hq

# Check the logs
sam logs --stack-name hospitality-hq --tail
```

You should see:
- How many reservations were checked
- Any new messages found
- Alerts posted to Slack

## Updating

After making code changes:

```bash
sam build && sam deploy
```

## Adjusting the Schedule

To change how often the monitor runs, redeploy with a different rate:

```bash
sam deploy --parameter-overrides ScheduleRate="rate(5 minutes)"
```

## Monitoring & Troubleshooting

**View recent logs:**
```bash
sam logs --stack-name hospitality-hq --tail
```

**Common issues:**

| Issue | Fix |
|-------|-----|
| No messages appearing in Slack | Check SlackChannelId is correct and bot is invited to channel |
| "Unauthorized" from Hospitable | Refresh your Hospitable API token |
| Claude API errors | Verify your API key at console.anthropic.com |
| Lambda timeout | Increase timeout in template.yaml (default 120s should be fine) |

## Costs

This system is extremely cheap to run:
- Lambda: Free tier covers ~1M invocations/month (we use ~3K)
- DynamoDB: Free tier covers 25 GB + 25 read/write units
- Claude API: ~$2-5/month depending on message volume
- Slack: Free plan works fine

## Tearing Down

To remove everything:

```bash
sam delete --stack-name hospitality-hq
```

This removes the Lambda, EventBridge rule, and DynamoDB table.
