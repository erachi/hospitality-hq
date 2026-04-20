"""AWS Lambda handler for the expense-capture workflow.

Subscribes to Slack `message.channels` events. When a file is posted in
the #expenses channel, we:

  1. 👀-react to the original message (so the submitter knows we saw it)
  2. Download the image
  3. Upload the original to S3 with Object Lock 7yr retention
  4. Run Claude vision OCR (tool use → structured JSON)
  5. Build an Expense with OCR + caption hints, persist it
  6. Post a confirmation card in-thread with property + category
     dropdowns and File-it / Split / Skip buttons

Interactions on the card (button clicks, dropdown changes) are the
subject of the follow-up PR — this handler wires up ingest only.

Signature verification reuses the Slack v0 scheme from task_handler.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs

from config import (
    EXPENSES_CHANNEL_ID,
    RECEIPT_RETENTION_DAYS,
    get_slack_signing_secret,
)
from expense_categories import suggest_from_merchant, valid_category_ids
from expense_models import (
    Allocation,
    CONFIDENCE_LOW,
    Expense,
    money_str,
    now_iso,
    year_for_transaction,
)
from expense_ocr import OCR_MODEL, extract_receipt
from expense_slack_client import (
    add_reaction,
    download_file,
    post_message,
)
from expense_slack_ui import build_error_card, build_extracted_card
from expense_store import ExpenseStore
from task_store import TaskStore


logger = logging.getLogger()
logger.setLevel(logging.INFO)

_MAX_TIMESTAMP_SKEW_SECONDS = 60 * 5

# Slack advertises up to 5 retries if our handler takes too long. We do
# all the work synchronously (OCR is ~3-6s) so a slow cold start can
# spill past the 3s ACK deadline. Fast-short-circuit on retry to avoid
# duplicate cards and double-ingest.
_RETRY_HEADER = "x-slack-retry-num"


# ─── Entry point ─────────────────────────────────────────────────────────


def slack_expenses_handler(event, context):
    """API Gateway Lambda proxy entry point."""
    raw_body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    headers = _lowercase_headers(event.get("headers", {}))

    # Slack retries our endpoint on any non-200 or slow reply. We don't
    # want to double-ingest — short-circuit with 200.
    if headers.get(_RETRY_HEADER):
        logger.info(
            f"Slack retry #{headers[_RETRY_HEADER]} "
            f"reason={headers.get('x-slack-retry-reason')} — acking only"
        )
        return _ok()

    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not _verify_signature(raw_body, timestamp, signature):
        logger.warning("Invalid Slack signature on expense webhook")
        return {"statusCode": 401, "body": "Invalid signature"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.info("Non-JSON body on expense events endpoint; ignoring")
        return _ok()

    # Slack URL verification handshake (one-time, on subscription setup).
    if payload.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": payload.get("challenge", ""),
        }

    # Events API wraps the inner event in event_callback.
    if payload.get("type") == "event_callback":
        try:
            _route_event(payload)
        except Exception as e:
            # Never 500 to Slack — retries are expensive and noisy. Log
            # with traceback and move on.
            logger.exception(f"Error handling expense event: {e}")
        return _ok()

    logger.info(f"Ignoring payload type: {payload.get('type')}")
    return _ok()


# ─── Event routing ───────────────────────────────────────────────────────


def _route_event(envelope: dict) -> None:
    inner = envelope.get("event", {}) or {}
    if inner.get("type") != "message":
        return
    if inner.get("subtype") != "file_share":
        return
    if inner.get("channel") != EXPENSES_CHANNEL_ID:
        logger.info(
            f"Ignoring file_share from channel {inner.get('channel')} "
            f"(expected {EXPENSES_CHANNEL_ID})"
        )
        return

    files = inner.get("files") or []
    if not files:
        return

    # Ingest each attached file independently. Normally there's one file
    # per message, but Slack does allow multiple.
    for file_obj in files:
        try:
            ingest_file(inner, file_obj)
        except Exception as e:
            logger.exception(
                f"Ingest failed for file {file_obj.get('id')}: {e}"
            )


# ─── Ingest pipeline ─────────────────────────────────────────────────────


def ingest_file(message: dict, file_obj: dict) -> Optional[Expense]:
    """Run the ingest pipeline for one file. Returns the Expense or None."""
    mimetype = (file_obj.get("mimetype") or "").lower()
    if not mimetype.startswith("image/"):
        logger.info(f"Skipping non-image file {file_obj.get('id')} mimetype={mimetype}")
        return None

    channel_id = message["channel"]
    message_ts = message["ts"]
    submitter_slack_id = message.get("user", "")
    caption = (message.get("text") or "").strip()

    # Idempotency: if a previous pass already processed this message,
    # the slack-index pointer exists. Bail out to avoid double-ingest on
    # retry paths we didn't short-circuit above.
    estore = ExpenseStore()
    if estore.get_expense_by_thread(message_ts) is not None:
        logger.info(f"Message ts={message_ts} already ingested — skipping")
        return None

    # Ack the submitter early so they see something happen even if OCR
    # is slow. Not fatal if this fails.
    try:
        add_reaction(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception as e:
        logger.warning(f"Failed to add eyes reaction: {e}")

    # Download the private Slack file with the bot token.
    download_url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not download_url:
        logger.error(f"file {file_obj.get('id')} has no download URL")
        _post_error_card(channel_id, message_ts, "Slack didn't return a download URL for that file.")
        return None

    try:
        image_bytes = download_file(download_url)
    except Exception as e:
        logger.exception(f"Failed to download image: {e}")
        _post_error_card(channel_id, message_ts, "Couldn't download the image from Slack.")
        return None

    image_sha256 = hashlib.sha256(image_bytes).hexdigest()

    # Run OCR before we allocate an expense id so that if OCR fails, we
    # don't burn an id on a dead record.
    try:
        ocr_payload = extract_receipt(image_bytes, mimetype)
    except Exception as e:
        logger.exception(f"OCR failed: {e}")
        _post_error_card(channel_id, message_ts, "OCR failed on that image. Try a clearer photo.")
        return None

    transaction_date = _as_date(ocr_payload.get("transaction_date"))
    year = year_for_transaction(transaction_date)
    expense_id = estore.next_id(year)

    # Upload the original with Object Lock governance retention. The
    # bucket has Object Lock enabled at creation but no default
    # retention, so we apply it here per-object.
    retention_until = _retention_until_iso()
    image_s3_key = estore.put_receipt_image(
        year=year,
        expense_id=expense_id,
        body=image_bytes,
        content_type=mimetype,
        retention_until=retention_until,
    )

    expense = _build_expense(
        expense_id=expense_id,
        submitter_slack_id=submitter_slack_id,
        channel_id=channel_id,
        message_ts=message_ts,
        caption=caption,
        image_s3_key=image_s3_key,
        image_sha256=image_sha256,
        ocr_payload=ocr_payload,
    )

    estore.put(expense)
    estore.put_slack_index(message_ts, expense.id)

    _post_confirmation_card(expense, channel_id, message_ts)
    return expense


def _build_expense(
    *,
    expense_id: str,
    submitter_slack_id: str,
    channel_id: str,
    message_ts: str,
    caption: str,
    image_s3_key: str,
    image_sha256: str,
    ocr_payload: dict,
) -> Expense:
    """Combine OCR output, caption hints, and infra state into an Expense."""
    merchant_name = (ocr_payload.get("merchant_name") or "").strip()
    transaction_date = _as_date(ocr_payload.get("transaction_date")) or _today_iso_date()
    total = _as_money(ocr_payload.get("total"))
    subtotal = _as_money_or_none(ocr_payload.get("subtotal"))
    tax = _as_money_or_none(ocr_payload.get("tax"))
    tip = _as_money_or_none(ocr_payload.get("tip"))
    currency = (ocr_payload.get("currency") or "USD").upper()
    payment_method = _opt_str(ocr_payload.get("payment_method"))

    # Rule-based merchant match wins; fall back to the LLM's suggestion.
    category_id = suggest_from_merchant(merchant_name)
    if not category_id:
        suggested = ocr_payload.get("suggested_category")
        if suggested in valid_category_ids():
            category_id = suggested

    # Caption-based property hint: look for a property slug or name token.
    property_id = _match_property_from_caption(caption)

    # If the caption named the property, pin the allocation to it now.
    # Otherwise the user picks from the card dropdown later.
    allocations: list[Allocation] = []
    if property_id and total:
        allocations = [Allocation.single(property_id, total)]

    extraction_conf = _norm_confidence(ocr_payload.get("extraction_confidence"))
    category_conf = _norm_confidence(ocr_payload.get("category_confidence"))

    needs_review = extraction_conf == CONFIDENCE_LOW or category_conf == CONFIDENCE_LOW
    review_reason = ocr_payload.get("needs_review_reason") if needs_review else None

    created = now_iso()
    return Expense(
        id=expense_id,
        submitter_slack_id=submitter_slack_id,
        merchant_name=merchant_name or "(unknown merchant)",
        transaction_date=transaction_date,
        total=total or "0.00",
        currency=currency,
        image_s3_key=image_s3_key,
        image_sha256=image_sha256,
        ocr_payload=ocr_payload,
        ocr_model=OCR_MODEL,
        slack_channel_id=channel_id,
        slack_thread_ts=message_ts,
        created_at=created,
        updated_at=created,
        subtotal=subtotal,
        tax=tax,
        tip=tip,
        category_id=category_id,
        property_id=property_id,
        payment_method=payment_method,
        notes=_opt_str(caption),
        ocr_extraction_confidence=extraction_conf,
        ocr_category_confidence=category_conf,
        needs_review=needs_review,
        review_reason=review_reason,
        allocations=allocations,
    )


def _post_confirmation_card(expense: Expense, channel_id: str, message_ts: str) -> None:
    properties = TaskStore().load_properties()
    blocks, fallback = build_extracted_card(expense=expense, properties=properties)
    result = post_message(
        channel=channel_id,
        text=fallback,
        blocks=blocks,
        thread_ts=message_ts,
    )
    if not result.get("ok"):
        logger.error(f"chat.postMessage failed: {result.get('error')}")


def _post_error_card(channel_id: str, message_ts: str, reason: str) -> None:
    blocks, fallback = build_error_card(reason=reason)
    try:
        post_message(
            channel=channel_id,
            text=fallback,
            blocks=blocks,
            thread_ts=message_ts,
        )
    except Exception as e:
        logger.warning(f"Failed to post error card: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────────


def _match_property_from_caption(caption: str) -> Optional[str]:
    """Return a property_id if the caption names a known property, else None.

    Match order: exact slug, exact name (case-insensitive), token prefix.
    """
    if not caption:
        return None
    text = caption.lower()
    properties = TaskStore().load_properties()
    # Exact slug/name (surrounded by non-word chars) wins first.
    for p in properties:
        slug = (p.get("slug") or "").lower()
        name = (p.get("name") or "").lower()
        if slug and slug in text.split():
            return p["id"]
        if name and name in text:
            return p["id"]
    # Token prefix (3+ chars) — lets "palm" match "The Palm Club".
    for token in text.split():
        if len(token) < 3:
            continue
        for p in properties:
            slug = (p.get("slug") or "").lower()
            if slug and slug.startswith(token):
                return p["id"]
            name = (p.get("name") or "").lower()
            if name and token in name.split():
                return p["id"]
    return None


def _as_date(value) -> Optional[str]:
    """Return a YYYY-MM-DD string or None if unparseable."""
    if not value:
        return None
    s = str(value).strip()
    # Accept ISO dates and the YYYY-MM-DD prefix of a datetime.
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            datetime.strptime(s[:10], "%Y-%m-%d")
            return s[:10]
        except ValueError:
            return None
    return None


def _today_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _as_money(value) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        return money_str(value)
    except Exception:
        return None


def _as_money_or_none(value) -> Optional[str]:
    return _as_money(value)


def _opt_str(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _norm_confidence(value) -> Optional[str]:
    if value in ("high", "medium", "low"):
        return value
    return None


def _retention_until_iso() -> str:
    """Object Lock RetainUntilDate for new receipt uploads.

    Floor of 7 years (IRS) — configurable via RECEIPT_RETENTION_DAYS.
    Returned in ISO-8601 UTC form that S3 accepts.
    """
    until = datetime.now(timezone.utc) + timedelta(days=RECEIPT_RETENTION_DAYS)
    return until.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ─── Slack signature verification ────────────────────────────────────────


def _verify_signature(raw_body: str, timestamp: str, signature: str) -> bool:
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > _MAX_TIMESTAMP_SKEW_SECONDS:
        return False

    secret = get_slack_signing_secret()
    if not secret:
        return False
    base_string = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    expected = (
        "v0=" + hmac.new(secret.encode("utf-8"), base_string, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def _lowercase_headers(headers: dict) -> dict:
    return {str(k).lower(): v for k, v in (headers or {}).items()}


def _ok() -> dict:
    return {"statusCode": 200, "body": ""}
