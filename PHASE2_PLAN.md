# Phase 2: Real-Time Webhook Architecture

## Why

Phase 1 polls every 15 minutes. A guest locked out at 11:03pm won't trigger an alert until 11:15pm at the earliest. With webhooks, that alert hits Slack within seconds.

## What Changes

```
PHASE 1 (current):
  EventBridge (15 min) → Lambda polls Hospitable → finds new messages → Slack

PHASE 2 (target):
  Guest sends message → Hospitable webhook → API Gateway → Lambda → Slack (seconds)
  EventBridge (1 hour) → Lambda polls Hospitable → catches anything missed (safety net)
```

## Hospitable Webhook Details

**Event:** `message.created`
**Delivery:** POST request with JSON payload
**Retry policy:** 5 retries with exponential backoff (1s, 5s, 10s, 1hr, 6hr)
**Source IPs:** `38.80.170.0/24`
**Security:** HMAC-SHA256 signature in `Signature` header

### Payload Structure

```json
{
  "id": "01GTKD6ZYFVQMR0RWP4HBBHNZC",
  "action": "message.created",
  "data": {
    "platform": "airbnb",
    "platform_id": 12345,
    "conversation_id": "abc-123",
    "reservation_id": "def-456",
    "body": "The AC isn't working and it's really hot",
    "sender_type": "guest",
    "sender_role": null,
    "sender": {
      "first_name": "John",
      "full_name": "John Smith",
      "locale": "en",
      "picture_url": "https://..."
    },
    "content_type": "text/plain",
    "source": "platform",
    "created_at": "2023-10-01T09:35:24Z",
    "attachments": []
  },
  "created": "2023-10-01T09:35:24Z",
  "version": "1.0"
}
```

## New AWS Architecture

```
                                    ┌─────────────────┐
Guest message → Hospitable ────────▶│  API Gateway     │
                webhook POST        │  (HTTPS endpoint)│
                                    └────────┬────────┘
                                             │
                                             ▼
                                    ┌─────────────────┐
                                    │  Lambda          │
                                    │  (webhook handler│──────▶ Slack #guest-alerts
                                    │   + classifier)  │
                                    └────────┬────────┘
                                             │
                                    ┌────────┴────────┐
                                    ▼                  ▼
                              ┌──────────┐     ┌─────────────┐
                              │ DynamoDB  │     │ Claude API   │
                              │(msg state)│     │(classify+draft)│
                              └──────────┘     └─────────────┘

Safety net (unchanged):
  EventBridge (hourly) → existing poll Lambda → catches missed webhooks
```

## New Resources Needed

| Resource | Type | Purpose |
|----------|------|---------|
| `hospitality-hq-webhook-api` | API Gateway (HTTP API) | HTTPS endpoint for Hospitable to POST to |
| `hospitality-hq-webhook` | Lambda Function | Processes incoming webhook events |
| _(existing)_ `hospitality-hq-monitor` | Lambda Function | Reduced to hourly safety-net polling |
| _(existing)_ `hospitality-hq-messages` | DynamoDB Table | Same state tracking table |

## Implementation Steps

### Step 1: Add API Gateway + Webhook Lambda to SAM template
- Create an HTTP API Gateway with a POST route `/webhook`
- New Lambda function `hospitality-hq-webhook` triggered by the API Gateway
- IP restriction to `38.80.170.0/24` (Hospitable's webhook IPs)
- HMAC-SHA256 signature verification in the Lambda

### Step 2: Build the webhook handler Lambda
- Receives the `message.created` payload
- Filters: only process `sender_type: "guest"` (ignore host/automated messages)
- Checks DynamoDB for duplicate delivery (using webhook `id` field)
- Fetches reservation details from Hospitable API (guest name, property, dates)
- Classifies + drafts response (reuses existing classifier.py)
- Posts to Slack
- Marks as processed in DynamoDB
- Returns 200 OK to Hospitable immediately

### Step 3: Reduce polling to hourly safety net
- Change EventBridge schedule from `rate(15 minutes)` to `rate(1 hour)`
- This catches any webhooks that failed delivery or were missed

### Step 4: Register the webhook with Hospitable
- Option A: Contact team-platform@hospitable.com to register the API Gateway URL
- Option B: Configure in Hospitable dashboard under Tools → Webhooks
- Set trigger to `message.created`
- Point to the API Gateway URL from Step 1

### Step 5: Verify end-to-end
- Send a test message from a guest account
- Confirm webhook arrives at API Gateway
- Confirm Slack alert appears within seconds
- Confirm safety-net poller still catches it on the hourly run (and deduplicates)

## Security Considerations

1. **IP Whitelisting:** API Gateway resource policy restricts to `38.80.170.0/24`
2. **Signature Verification:** HMAC-SHA256 validation on every request
3. **Idempotency:** DynamoDB deduplication using webhook `id` to handle retries
4. **Rate limiting:** API Gateway throttling as a safety measure

## Cost Impact

Minimal change:
- API Gateway: Free tier covers 1M requests/month (we'll use ~hundreds)
- Lambda: Same free tier, just triggered differently
- Polling Lambda drops from 96 to 24 invocations/day
- Net effect: roughly the same ~$3-6/month

## Timeline

| Task | Effort | Dependency |
|------|--------|------------|
| Add API Gateway + Lambda to SAM template | 30 min | None |
| Build webhook handler Lambda | 45 min | Template |
| Deploy and get API Gateway URL | 5 min | Handler built |
| Register webhook with Hospitable | 10 min | URL available |
| Reduce poller to hourly | 5 min | Webhook confirmed working |
| End-to-end testing | 30 min | Everything deployed |

**Total: ~2 hours of implementation**

## Bonus: Future Phase 3 Ideas

- **review.created webhook** → Auto-draft review responses, post to `#reviews` channel
- **reservation.created webhook** → Instant notification of new bookings
- **Slack interactivity** → "Send" button in Slack posts that triggers the Hospitable send-message API directly (requires a small Slack app with interactive components)
