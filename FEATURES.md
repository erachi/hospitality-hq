# Feature Development Framework

Every new feature in Hospitality HQ follows this lifecycle. The goal is to move quickly while maintaining reliability — we're running a system that directly affects guest experience.

## Lifecycle: Ideation → Design → Develop → Test → Deploy → Monitor

### 1. Ideation

Start here when you have an idea or notice a gap.

**Capture it as a GitHub Issue** with:
- **Problem:** What's happening (or not happening) that should be different?
- **Impact:** How does this affect guest experience, host efficiency, or system reliability?
- **Rough idea:** What might the solution look like?

Label it: `idea`, plus one of `guest-experience`, `operations`, `infrastructure`.

**Examples:**
- "Guests asking about pool heating don't get responses fast enough" → guest-experience
- "No way to track which issues are resolved vs open" → operations
- "Polling every 15 min misses urgent messages" → infrastructure

### 2. Design

Before writing code, answer these questions in the issue:

- **What data do we need?** Which Hospitable API endpoints, what context?
- **What's the trigger?** Scheduled, webhook, manual?
- **What's the output?** Slack message, DynamoDB record, file, etc.?
- **What could go wrong?** API failures, rate limits, edge cases?
- **Does this touch guest communication?** If yes, how do we ensure human approval?

For significant features, create a `PHASE_N_PLAN.md` document (like PHASE2_PLAN.md).

### 3. Develop

```bash
# Create a feature branch
git checkout -b feature/your-feature-name

# Write tests FIRST
# Add test file: tests/test_your_feature.py

# Then implement
# Add/modify source files in src/

# Run tests locally
pytest tests/ -v

# Commit with clear messages
git add .
git commit -m "feat: add your feature description"
```

**Branch naming:**
- `feature/description` — new functionality
- `fix/description` — bug fixes
- `infra/description` — deployment, CI, config changes

**Commit message prefixes:**
- `feat:` — new feature
- `fix:` — bug fix
- `infra:` — infrastructure/deployment
- `test:` — test additions/changes
- `docs:` — documentation only

### 4. Test

Tests are non-negotiable. Every feature needs:

- **Unit tests** — mock external APIs, test logic in isolation
- **Edge case tests** — empty messages, API failures, malformed data
- **Integration test** (where practical) — test the flow end-to-end with mocks

```bash
# Run with coverage
pytest tests/ -v --cov=src --cov-report=term-missing

# Coverage must stay above 60%
```

**Testing patterns used in this project:**
- `moto` for AWS services (DynamoDB, SSM)
- `responses` for HTTP mocking (Slack API, Hospitable API)
- `unittest.mock.patch` for internal module mocking (Claude API client)

### 5. Deploy

```bash
# Push your branch
git push origin feature/your-feature-name

# CI runs automatically — check GitHub Actions

# Create a PR to main
# Describe what changed and why

# After PR approval + CI passes, merge to main

# Deploy
sam build && sam deploy
```

**For infrastructure changes** (new Lambda, DynamoDB table, API Gateway):
- Update `template.yaml`
- Test with `sam build` locally first
- Deploy creates/updates resources automatically

### 6. Monitor

After deploying, watch for issues:

```bash
# Tail logs
sam logs --stack-name hospitality-hq --tail

# Check recent invocations
aws lambda invoke --function-name hospitality-hq-monitor --payload '{}' output.json && cat output.json
```

Check `#guest-alerts` in Slack to confirm alerts are flowing correctly.

---

## Feature Backlog

Planned features, roughly prioritized:

### Phase 2: Real-Time Webhooks
- **Status:** Planned (see PHASE2_PLAN.md)
- **What:** Replace 15-min polling with instant webhook-triggered alerts
- **Why:** Guests with urgent issues get help in seconds, not minutes

### Phase 3: Review Response Drafting
- **Status:** Idea
- **What:** Auto-draft responses to guest reviews, post to Slack for approval
- **Why:** Reviews are critical for ranking; fast, thoughtful responses matter

### Phase 4: Issue Tracker Dashboard
- **Status:** Idea
- **What:** A simple web dashboard showing open/resolved issues per property
- **Why:** Gives VJ and Maggie a bird's-eye view of guest experience health

### Phase 5: Proactive Monitoring
- **Status:** Idea
- **What:** Flag reservations that might have issues before guests report them
- **Why:** Anticipate problems (e.g., large group + pool heating not requested)

### Phase 6: Slack Interactivity
- **Status:** Idea
- **What:** One-click "Send" button in Slack to approve and send drafts directly
- **Why:** Reduces friction from "copy draft → go to Hospitable → paste → send"

---

## Adding a New Property

When a new property is added to Hospitable:

1. Get the property UUID from Hospitable
2. Update `PROPERTY_UUIDS` in the SAM template or Lambda env vars
3. Redeploy: `sam build && sam deploy`
4. The monitor will automatically start watching the new property
