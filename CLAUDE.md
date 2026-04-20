# Hospitality HQ

Three workflows for short-term rental ops, sharing a Slack app and AWS stack:

1. **Guest monitoring** — watches Hospitable for new guest messages, classifies them, drafts replies, posts to `#guest-alerts` for human approval.
2. **Task management** — `/task` slash command + interactive Slack cards for VJ and Maggie to track internal work (fixes, compliance, marketing). Backed by S3.
3. **Expense capture** — receipt photo → `#expenses` → OCR via Claude vision → Schedule E category → filed as JSON on S3 with receipt image under Object Lock. See `EXPENSES.md` for the design; capture + classify is the MVP, year-end export is v2.

## What This Does

### Guest monitoring
Monitors guest conversations across properties, classifies issues by urgency, drafts AI-powered responses, and posts them to Slack for human approval. **No messages are ever sent to guests without explicit approval from VJ or Maggie.**

### Task management
Internal tracker used by VJ and Maggie only. `/task` opens a modal; cards post to `#tasks` with Mark-done / Start / Block / Swap-assignee buttons; thread replies become comments; a daily cron DMs overdue + due-today digests. Tasks live as JSON objects in S3 (versioning on). See `TASKS_CHEATSHEET.md` for user-facing docs.

### Expense capture
Any team member drops a receipt photo in `#expenses`; Claude vision extracts merchant / date / total, suggests a Schedule E category, and posts a Block Kit card for one-click confirmation. Categories map 1:1 to IRS Schedule E lines (no invented subcategories). Each expense is one JSON at `s3://hospitality-hq-expenses/expenses/<year>/<id>.json`; original receipt images live under `receipts/` with 7-year Object Lock GOVERNANCE retention applied per-object. See `EXPENSES.md` for the design doc.

## Architecture

- **AWS Lambda** (Python 3.12) — monitoring + webhook + task handlers
- **EventBridge** — hourly monitor, daily task escalator
- **DynamoDB** — guest-message dedup, thread mapping, thread logs
- **S3** — one JSON per task, static config (properties, users), versioned
- **SSM Parameter Store** — stores API secrets (encrypted)
- **Hospitable API** — property data, reservations, messages, knowledge hub
- **Claude API** — issue classification (Haiku) + response drafting (Sonnet)
- **Slack API** — posts formatted alerts to #guest-alerts, task cards to #tasks

## Properties

- Villa Bougainvillea (`f8236d9d-988a-4192-9d16-2927b0b9ad8e`) — Scottsdale, AZ
- The Palm Club (`3278e9cb-9239-487f-aa51-cbfbaf4b7570`) — Scottsdale, AZ

## Project Structure

```
hospitality_hq/
├── src/                      # Lambda function source
│   ├── handler.py            # Guest monitor orchestrator
│   ├── config.py             # Config + SSM secret fetching
│   ├── hospitable_client.py  # Hospitable API wrapper
│   ├── classifier.py         # Claude-powered classification + drafting
│   ├── slack_notifier.py     # Guest-alert Slack formatting + posting
│   ├── state_tracker.py      # DynamoDB dedup tracking
│   ├── thread_handler.py     # Slack thread replies on guest alerts
│   ├── task_models.py        # Task / Comment dataclasses + enums
│   ├── task_store.py         # S3-backed CRUD for tasks
│   ├── task_slack_ui.py      # Block Kit for task modal / card / list
│   ├── task_slack_client.py  # Thin Slack Web API wrapper (tasks)
│   ├── task_handler.py       # Slash commands, buttons, thread replies
│   ├── task_escalator.py     # Daily overdue-task DM digest
│   ├── expense_models.py     # Expense / Allocation dataclasses + helpers
│   ├── expense_store.py      # S3-backed CRUD for expenses + receipt images
│   ├── expense_categories.py # Schedule E taxonomy + merchant rule matching
│   └── requirements.txt      # Python dependencies
├── tests/                    # Test suite (pytest + moto + responses)
├── seed/                     # Static config
│   ├── properties.json       # Shared by tasks and expenses
│   ├── users.json            # Tasks workflow only
│   ├── expense_categories.json     # Schedule E taxonomy
│   └── expense_merchant_patterns.json  # Merchant → category rules
├── .github/workflows/ci.yml  # GitHub Actions CI
├── template.yaml             # AWS SAM deployment template
├── setup-secrets.sh          # Script to store secrets in SSM
├── setup-tasks.sh            # Script to seed S3 with task config
├── TASKS_CHEATSHEET.md       # User-facing cheatsheet for VJ/Maggie
├── ARCHITECTURE.md           # Detailed system design
├── DEPLOY.md                 # Deployment guide
├── PHASE2_PLAN.md            # Webhook-based real-time architecture
├── EXPENSES.md               # Expense-capture workflow design doc
└── FEATURES.md               # Feature development framework
```

## Key Conventions

- **Never send guest messages** without explicit VJ or Maggie approval
- **Secrets** live in SSM Parameter Store, never in env vars or config files
- **Tests** must pass before merging to main (enforced by CI)
- **Coverage** minimum: 60% (enforced by CI)
- **All external API calls** are mocked in tests (no real API calls in CI)

## Commands

```bash
# Run tests locally
pip install -r src/requirements.txt -r tests/requirements-test.txt
pytest tests/ -v

# Deploy
sam build && sam deploy

# Store/rotate secrets
./setup-secrets.sh <hospitable-token> <claude-key> <slack-token>

# Seed the tasks bucket with properties + users
./setup-tasks.sh [bucket-name]

# Invoke manually
aws lambda invoke --function-name hospitality-hq-monitor --payload '{}' output.json && cat output.json
aws lambda invoke --function-name hospitality-hq-task-escalator --payload '{}' output.json && cat output.json

# View logs
sam logs --stack-name hospitality-hq --tail
```

## Development Workflow

See FEATURES.md for the full feature development lifecycle.

1. Create a feature branch from `main`
2. Write tests first, then implement
3. All tests must pass locally before pushing
4. Push triggers CI — tests run automatically
5. PR to `main` requires passing CI
6. After merge, deploy with `sam build && sam deploy`
