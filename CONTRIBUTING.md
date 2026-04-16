# Contributing to Hospitality HQ

This project supports concurrent development by multiple AI agents and human contributors. Follow these rules to avoid collisions and keep the codebase healthy.

## Branch Strategy

Always work on a feature branch — never commit directly to `main`.

```
feature/description   — new functionality
fix/description       — bug fixes
infra/description     — deployment, CI, config changes
```

**Commit message prefixes:**
- `feat:` — new feature
- `fix:` — bug fix
- `infra:` — infrastructure/deployment
- `test:` — test additions/changes
- `docs:` — documentation only

## File Ownership Boundaries

To prevent merge conflicts when multiple agents work simultaneously, respect these module boundaries:

| Module | Files | Owner Scope |
|--------|-------|-------------|
| **Handler/Orchestration** | `src/handler.py` | Message polling flow, reservation iteration |
| **Classification** | `src/classifier.py` | Claude API calls, prompt templates, classification logic |
| **Hospitable Client** | `src/hospitable_client.py` | All Hospitable API interactions |
| **Slack Notifications** | `src/slack_notifier.py` | Slack message formatting and posting |
| **State Tracking** | `src/state_tracker.py` | DynamoDB read/write, deduplication |
| **Config** | `src/config.py` | SSM secrets, environment variables, constants |
| **Infrastructure** | `template.yaml` | SAM template, AWS resources |
| **CI** | `.github/workflows/ci.yml` | GitHub Actions pipeline |

**Rules:**
1. If your feature touches only one module, work in that module's files.
2. If your feature spans multiple modules, coordinate — don't refactor shared interfaces without updating all callers.
3. New features should get their own new file(s) when possible rather than expanding existing ones.
4. Tests mirror source: `src/foo.py` → `tests/test_foo.py`. Each module owns its own test file.

## Adding New Modules

When adding a new capability (e.g., webhook handler, dashboard API):

1. Create a new source file: `src/your_module.py`
2. Create a matching test file: `tests/test_your_module.py`
3. Import and wire it into `handler.py` or create a new Lambda entry point
4. Update `template.yaml` if new AWS resources are needed
5. Update `README.md` project structure section

## Shared Resources — Be Careful

These files are touched by many features. Edit them minimally and with care:

- **`src/config.py`** — Add new config values at the bottom of the relevant section
- **`template.yaml`** — Add new resources; don't restructure existing ones
- **`tests/conftest.py`** — Add new fixtures; don't modify existing ones
- **`README.md`** — Update the relevant section only

## Testing Requirements

Every PR must pass CI. The pipeline runs:

```bash
pytest tests/ -v --cov=src --cov-report=term-missing
```

**Requirements:**
- All existing tests must continue to pass
- New code must have tests
- Coverage must stay above 60%
- Mock all external services (AWS via `moto`, HTTP via `responses`, Claude via `unittest.mock.patch`)

**Testing patterns:**
- `moto` — DynamoDB, SSM Parameter Store
- `responses` — Slack API, Hospitable API HTTP calls
- `unittest.mock.patch` — Claude/Anthropic client

## The Golden Rule

**No message is ever sent to a guest without explicit approval from VJ or Maggie.**

Any feature that could result in outbound guest communication must include a human-approval step (currently: post draft to Slack, wait for manual send via Hospitable).

## PR Checklist

Before opening a PR:

- [ ] Feature branch with correct naming convention
- [ ] Tests written and passing locally (`pytest tests/ -v`)
- [ ] No secrets or API keys in code (use `src/config.py` → SSM)
- [ ] `README.md` updated if project structure changed
- [ ] Commit messages use correct prefixes
- [ ] No direct guest messaging without human approval gate
