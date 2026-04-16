# Hospitality HQ — System Architecture

## Overview

An always-on guest monitoring system that watches Hospitable conversations, classifies issues, drafts responses, and posts them to Slack for human approval before anything reaches a guest.

**Core Rule: NEVER message a guest unless VJ or Maggie explicitly approve it.**

## Properties Monitored

| Property | UUID | Location |
|----------|------|----------|
| Villa Bougainvillea | `f8236d9d-988a-4192-9d16-2927b0b9ad8e` | 5120 N 87th St, Scottsdale, AZ |
| The Palm Club | `3278e9cb-9239-487f-aa51-cbfbaf4b7570` | 7426 E Moreland St, Scottsdale, AZ |

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  EventBridge │────▶│   AWS Lambda      │────▶│    Slack     │
│  (every 15m) │     │  (Python 3.12)   │     │ #guest-alerts│
└─────────────┘     └──────────────────┘     └─────────────┘
                          │       │
                    ┌─────┘       └─────┐
                    ▼                   ▼
              ┌──────────┐      ┌─────────────┐
              │Hospitable│      │  Claude API  │
              │   API    │      │ (Haiku/Sonnet)│
              └──────────┘      └─────────────┘
                    │
                    ▼
              ┌──────────┐
              │ DynamoDB  │
              │(msg state)│
              └──────────┘
```

## Flow (each 15-minute run)

1. **Poll** — Fetch active/upcoming reservations from Hospitable (status: accepted, checkpoint)
2. **Detect** — For each reservation, fetch messages and compare against DynamoDB to find new guest messages
3. **Classify** — Send new messages to Claude Haiku for fast classification:
   - 🔴 URGENT: Lockouts, broken AC/plumbing, safety issues
   - 🟠 COMPLAINT: Cleanliness, noise, missing items
   - 🟡 PRE-ARRIVAL: Check-in questions, directions, special requests
   - 🔵 GENERAL: Other inquiries
   - 🟢 POSITIVE: Compliments, thank-yous
4. **Enrich** — Pull property Knowledge Hub context relevant to the issue
5. **Draft** — Generate a suggested response using Claude Sonnet with full property context
6. **Notify** — Post formatted alert to Slack #guest-alerts with:
   - Guest name, property, dates
   - Classification + urgency
   - Original message
   - Draft response
7. **Track** — Mark messages as processed in DynamoDB

## AWS Resources

| Resource | Type | Purpose |
|----------|------|---------|
| `hospitality-hq-monitor` | Lambda Function | Core monitoring logic |
| `hospitality-hq-schedule` | EventBridge Rule | 15-minute trigger |
| `hospitality-hq-messages` | DynamoDB Table | Processed message tracking |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `HOSPITABLE_API_TOKEN` | Hospitable API bearer token |
| `ANTHROPIC_API_KEY` | Claude API key |
| `SLACK_BOT_TOKEN` | Slack bot OAuth token |
| `SLACK_CHANNEL_ID` | #guest-alerts channel ID |
| `PROPERTY_UUIDS` | Comma-separated property UUIDs |

## Cost Estimate

At 2 properties with typical guest volume:
- Lambda: ~2,880 invocations/month (every 15 min) = ~$0.01
- DynamoDB: Minimal reads/writes = ~$0.25
- Claude API: ~$2-5/month (Haiku for classification, Sonnet for drafts)
- **Total: ~$3-6/month**
