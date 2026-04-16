# Hospitality HQ

An always-on guest experience management system for short-term rental properties. Monitors guest conversations in [Hospitable](https://hospitable.com), classifies issues by urgency, drafts AI-powered responses, and posts them to Slack for human review and approval.

Built for VJ's Scottsdale vacation rental portfolio.

## The Problem

Managing guest communications across multiple properties means messages can slip through the cracks — a guest locked out at midnight, a broken AC during an Arizona summer, a pre-arrival question that goes unanswered. Every missed or slow response hurts the guest experience and your reviews.

## The Solution

Hospitality HQ watches every guest conversation 24/7 (even when your laptop is off), classifies each message by type and urgency, drafts a contextual response using property-specific knowledge, and posts the whole package to Slack where you or your team can review and send with confidence.

**Rule #1: No message is ever sent to a guest without explicit human approval.**

## How It Works

```
Guest sends message
        │
        ▼
  Hospitable API
        │
        ▼
  AWS Lambda (every 15 min)
   ├── Fetches new messages
   ├── Classifies: 🔴 Urgent │ 🟠 Complaint │ 🟡 Pre-arrival │ 🔵 General │ 🟢 Positive
   ├── Pulls property context from Knowledge Hub
   ├── Drafts response via Claude API
   └── Posts to Slack #guest-alerts
        │
        ▼
  VJ or Maggie reviews → approves → sends via Hospitable
```

## Properties

| Property | Type | Location | Sleeps |
|----------|------|----------|--------|
| **Villa Bougainvillea** | 5BR/3BA House | Scottsdale, AZ | 16 |
| **The Palm Club** | 4BR/3BA House | Scottsdale, AZ | 16 |

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Compute | AWS Lambda (Python 3.12) | Core monitoring logic |
| Scheduler | AWS EventBridge | Triggers Lambda every 15 minutes |
| State | AWS DynamoDB | Tracks processed messages (dedup) |
| Secrets | AWS SSM Parameter Store | Encrypted API key storage |
| PMS | Hospitable API | Property data, reservations, messages, knowledge hub |
| AI | Claude API (Haiku + Sonnet) | Issue classification + response drafting |
| Notifications | Slack API | Alert delivery to #guest-alerts |
| CI | GitHub Actions | Tests run on every push and PR |
| IaC | AWS SAM | Infrastructure as code (template.yaml) |

## Project Structure

```
hospitality_hq/
├── src/                          # Lambda function source
│   ├── handler.py                # Main orchestrator — poll → classify → draft → notify
│   ├── config.py                 # Configuration + SSM secret fetching
│   ├── hospitable_client.py      # Hospitable API wrapper
│   ├── classifier.py             # Claude-powered classification + response drafting
│   ├── slack_notifier.py         # Slack alert formatting + posting
│   ├── state_tracker.py          # DynamoDB message deduplication
│   └── requirements.txt          # Python dependencies
│
├── tests/                        # Test suite (mocked external dependencies)
│   ├── conftest.py               # Shared fixtures and test config
│   ├── test_handler.py           # Handler orchestration tests
│   ├── test_classifier.py        # Classification + drafting tests
│   ├── test_state_tracker.py     # DynamoDB state tracking tests
│   └── test_slack_notifier.py    # Slack notification tests
│
├── .github/workflows/ci.yml     # GitHub Actions CI pipeline
├── template.yaml                 # AWS SAM deployment template
├── setup-secrets.sh              # Store API keys in SSM Parameter Store
├── pytest.ini                    # Pytest configuration
│
├── CLAUDE.md                     # Project conventions for AI agents
├── CONTRIBUTING.md               # Agent coordination and development rules
├── ARCHITECTURE.md               # Detailed system design
├── DEPLOY.md                     # Step-by-step deployment guide
├── FEATURES.md                   # Feature development lifecycle + backlog
└── PHASE2_PLAN.md                # Webhook-based real-time architecture plan
```

## Quick Start

### Prerequisites

- AWS CLI configured (`aws configure`)
- AWS SAM CLI (`brew install aws-sam-cli`)
- Python 3.12
- A Hospitable account (Host plan or higher)
- A Slack workspace with a `#guest-alerts` channel
- A Claude API key from [console.anthropic.com](https://console.anthropic.com)

### 1. Store Secrets

```bash
./setup-secrets.sh <hospitable-token> <claude-api-key> <slack-bot-token>
```

### 2. Deploy

```bash
sam build && sam deploy
```

### 3. Test

```bash
aws lambda invoke --function-name hospitality-hq-monitor --payload '{}' output.json && cat output.json
```

### 4. Run Tests Locally

```bash
pip install -r src/requirements.txt -r tests/requirements-test.txt
pytest tests/ -v
```

## Classification Categories

| Emoji | Category | Urgency | Examples |
|-------|----------|---------|----------|
| 🔴 🔧 | URGENT_MAINTENANCE | HIGH | Lockout, broken AC, plumbing, no hot water, WiFi down |
| 🟠 😤 | COMPLAINT | MEDIUM | Cleanliness, noise, missing amenities |
| 🟡 ✈️ | PRE_ARRIVAL | MEDIUM | Check-in questions, directions, early arrival |
| 🔵 💬 | GENERAL | LOW | Pool heating, vendor requests, late checkout |
| 🟢 ⭐ | POSITIVE | LOW | Compliments, thank-yous |

## Roadmap

See [FEATURES.md](FEATURES.md) for the full backlog and development lifecycle.

- **Phase 1** ✅ — Polling-based monitoring with Slack alerts
- **Phase 2** 📋 — Real-time webhooks (instant alerts, see [PHASE2_PLAN.md](PHASE2_PLAN.md))
- **Phase 3** 💡 — Review response drafting
- **Phase 4** 💡 — Issue tracker dashboard
- **Phase 5** 💡 — Proactive monitoring (anticipate issues before guests report)
- **Phase 6** 💡 — Slack interactivity (one-click send from Slack)

## Current State (as of April 2026)

### What's Deployed and Working
- Lambda function deployed to AWS (`hospitality-hq-monitor`)
- EventBridge schedule running every 15 minutes
- DynamoDB table for message dedup (`hospitality-hq-messages`)
- Secrets stored in SSM Parameter Store (`/hospitality-hq/*`)
- Slack workspace "Hospitality HQ" with `#guest-alerts` channel (ID: `C0ARUGVP8P7`)

### What Needs Attention
- **API keys need rotation** — original keys were exposed during setup; rotate all three (Hospitable, Claude, Slack) and update via `setup-secrets.sh`
- **End-to-end test pending** — Lambda found 8 reservations and 44 messages on first run but Claude API credits were empty at the time; needs a clean test run
- **Phase 2 webhook implementation** — plan is written, ready to build

### Key Decisions Made
- Secrets in SSM Parameter Store (not env vars, not Secrets Manager)
- Haiku for classification (fast, cheap), Sonnet for drafting (quality)
- Polling safety net will remain even after webhooks are added
- No guest messages sent without VJ or Maggie approval — ever

## Cost

~$3-6/month total:
- Lambda: ~$0.01 (well within free tier)
- DynamoDB: ~$0.25 (well within free tier)
- Claude API: ~$2-5 (depends on message volume)

## License

Private project for VJ's rental portfolio. Public repo for transparency and portfolio purposes.
