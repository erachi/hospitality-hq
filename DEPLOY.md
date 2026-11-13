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

### Architecture note: one Slack app serves every workflow

The Hospitality HQ workspace runs a **single Slack app** — "Hospitality HQ Bot".
Because a Slack app can only have **one Event Subscriptions URL** and **one
Interactivity URL**, all Slack traffic across every workflow (guest alerts,
stay channels, receipts, tasks) flows through two unified Lambda endpoints:

| Workflow | Events (message.*) | Interactivity (buttons + modals) | Slash commands |
|---|---|---|---|
| Guest alerts | `/slack/events` | — | — |
| Stay channels | `/slack/events` | `/slack/interactions` | — |
| Receipts | `/slack/events` | `/slack/interactions` | — |
| Tasks | `/slack/events` | `/slack/interactions` | `/task` → `/slack/tasks/commands` |

`thread_handler.slack_events_handler` dispatches based on channel ID +
subtype (for events) and `action_id` / `callback_id` (for interactions).
The `/slack/expenses/events`, `/slack/expenses/interactions`,
`/slack/tasks/events`, and `/slack/tasks/interactions` routes in
`template.yaml` are legacy from an earlier multi-app design and are
unreachable from Slack with the current single-app setup — safe to leave
in place as dead code, delete in a future cleanup PR. The task slash
command keeps its own URL (`/slack/tasks/commands`) since Slack
configures slash-command Request URLs per-command.

### Scopes

Go to [api.slack.com/apps](https://api.slack.com/apps) → **Hospitality HQ Bot** → **OAuth & Permissions** and make sure these Bot Token Scopes are all present:

- `chat:write` — post messages
- `channels:read` — find channel IDs
- `channels:history` — read `#guest-alerts` message events
- `groups:write` — create per-stay private channels + invite users *(stay channels)*
- `groups:read` — list private channels *(stay channels, receipts)*
- `groups:history` — read private channel messages (stay-channel agent + `#receipts` file uploads)
- `pins:write` — pin the stay-channel intro message
- `files:read` — download uploaded receipt images

Re-install to workspace whenever you add scopes.

### Event Subscriptions

In **Event Subscriptions**, set Request URL to:
```
https://<your-api-id>.execute-api.<region>.amazonaws.com/prod/slack/events
```
Subscribe to bot events:
- `message.channels` — public channels (guest alerts)
- `message.groups` — private channels (stay channels, receipts)

### Interactivity & Shortcuts

Turn on **Interactivity**, set Request URL to:
```
https://<your-api-id>.execute-api.<region>.amazonaws.com/prod/slack/interactions
```
This handles stay-channel draft buttons *and* expense-card buttons.

### Channels

1. `#guest-alerts` — public, bot invited. Set `SlackChannelId` parameter to this ID.
2. `#receipts` — private, bot + VJ + Maggie. Set `ExpensesChannelId` parameter to this ID.
3. Stay channels are auto-created by the monitor Lambda — no manual setup.

Get channel IDs: right-click channel → *Copy* → *Copy link* → ID is the last URL segment, starts with `C...`.

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

Normal path: **merge to `main`**. The `Deploy` GitHub Actions workflow runs on every successful CI run on `main`, assumes the `hospitality-hq-github-deploy` role via OIDC, and runs `sam build && sam deploy`. See [`bootstrap/README.md`](bootstrap/README.md) for the one-time OIDC setup.

Break-glass local deploy (only when CD is broken):

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
