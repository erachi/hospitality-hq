# Hospitality HQ

An always-on guest monitoring and response system for short-term rental properties managed through Hospitable.

## What This Does

Monitors guest conversations across properties, classifies issues by urgency, drafts AI-powered responses, and posts them to Slack for human approval. **No messages are ever sent to guests without explicit approval from VJ or Maggie.**

## Architecture

- **AWS Lambda** (Python 3.12) — core monitoring logic
- **EventBridge** — triggers Lambda on a schedule
- **DynamoDB** — tracks processed messages to avoid duplicates
- **SSM Parameter Store** — stores API secrets (encrypted)
- **Hospitable API** — property data, reservations, messages, knowledge hub
- **Claude API** — issue classification (Haiku) + response drafting (Sonnet)
- **Slack API** — posts formatted alerts to #guest-alerts

## Properties

- Villa Bougainvillea (`f8236d9d-988a-4192-9d16-2927b0b9ad8e`) — Scottsdale, AZ
- The Palm Club (`3278e9cb-9239-487f-aa51-cbfbaf4b7570`) — Scottsdale, AZ

## Project Structure

```
hospitality_hq/
├── src/                    # Lambda function source
│   ├── handler.py          # Main orchestrator
│   ├── config.py           # Config + SSM secret fetching
│   ├── hospitable_client.py # Hospitable API wrapper
│   ├── classifier.py       # Claude-powered classification + drafting
│   ├── slack_notifier.py   # Slack alert formatting + posting
│   ├── state_tracker.py    # DynamoDB dedup tracking
│   └── requirements.txt    # Python dependencies
├── tests/                  # Test suite
│   ├── conftest.py         # Shared fixtures
│   ├── test_handler.py     # Handler orchestration tests
│   ├── test_classifier.py  # Classification + drafting tests
│   ├── test_state_tracker.py # DynamoDB state tests
│   └── test_slack_notifier.py # Slack notification tests
├── .github/workflows/ci.yml # GitHub Actions CI
├── template.yaml           # AWS SAM deployment template
├── setup-secrets.sh        # Script to store secrets in SSM
├── ARCHITECTURE.md         # Detailed system design
├── DEPLOY.md               # Deployment guide
├── PHASE2_PLAN.md          # Webhook-based real-time architecture
└── FEATURES.md             # Feature development framework
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

# Invoke manually
aws lambda invoke --function-name hospitality-hq-monitor --payload '{}' output.json && cat output.json

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
