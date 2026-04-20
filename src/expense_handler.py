"""AWS Lambda handler for the expense-capture workflow.

Handles two kinds of Slack traffic from API Gateway:

  1. Events  (POST /slack/expenses/events, JSON)
     - message.channels with subtype=file_share in #expenses
     - Each image: 👀-react, download, S3 upload with 7-yr Object Lock,
       Claude vision OCR, persist the Expense, post confirmation card.

  2. Interactive components  (POST /slack/expenses/interactions,
     form-urlencoded with a "payload" JSON field)
     - block_actions from the confirmation card:
         * File it    → mark filed, render filed card in place
         * Skip       → mark personal, render skipped card
         * Split      → v2 placeholder, ephemeral reply
         * Property   → update allocation, re-render card
         * Category   → update category, re-render card

Card updates go through `response_url` rather than chat.update, so we
don't need to persist the card's message_ts on the Expense.

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
from expense_categories import (
    get_category,
    suggest_from_merchant,
    valid_category_ids,
)
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
    post_response_url,
)
from expense_slack_ui import (
    ACTION_CATEGORY_SELECT,
    ACTION_FILE_IT,
    ACTION_PROPERTY_SELECT,
    ACTION_SKIP,
    ACTION_SPLIT,
    build_error_card,
    build_extracted_card,
    build_filed_card,
    build_skipped_card,
    expense_id_from_block_id,
)
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

    content_type = (headers.get("content-type") or "").lower()

    try:
        # Interactive components arrive as form-urlencoded with a
        # single `payload` field containing JSON.
        if "application/x-www-form-urlencoded" in content_type:
            form = _parse_form(raw_body)
            if "payload" not in form:
                logger.info("form-urlencoded body without payload field; ignoring")
                return _ok()
            payload = json.loads(form["payload"])
            try:
                return _route_interaction(payload)
            except Exception as e:
                logger.exception(f"Error handling interaction: {e}")
                return _ok()

        # Otherwise the body is raw JSON from the Events API.
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        logger.info("Non-JSON / unparseable body on expenses endpoint; ignoring")
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


# ─── Interactions (block_actions from the confirmation card) ────────────


def _route_interaction(payload: dict) -> dict:
    """Route an interactive_components payload. Returns the HTTP response."""
    kind = payload.get("type")

    if kind != "block_actions":
        logger.info(f"Ignoring interaction type: {kind}")
        return _ok()

    actions = payload.get("actions") or []
    if not actions:
        return _ok()

    # Slack sends one payload per click even though `actions` is a list;
    # for dropdowns it still contains exactly one element.
    action = actions[0]
    action_id = action.get("action_id", "")
    expense_id = _expense_id_from_action(action)
    response_url = payload.get("response_url", "")
    actor_slack_id = (payload.get("user") or {}).get("id", "")

    if not expense_id:
        logger.warning(f"Interaction {action_id} with no expense id")
        _ephemeral(response_url, "_Couldn't find the expense id on that button. Try again?_")
        return _ok()

    store = ExpenseStore()
    expense = store.get(expense_id)
    if not expense:
        logger.warning(f"Interaction {action_id} for unknown expense {expense_id}")
        _ephemeral(response_url, f"_Expense `{expense_id}` no longer exists._")
        return _ok()

    try:
        if action_id == ACTION_FILE_IT:
            _handle_file_it(expense, store, response_url, actor_slack_id)
        elif action_id == ACTION_SKIP:
            _handle_skip(expense, store, response_url, actor_slack_id)
        elif action_id == ACTION_SPLIT:
            _handle_split_placeholder(response_url)
        elif action_id == ACTION_PROPERTY_SELECT:
            _handle_property_select(expense, store, action, response_url)
        elif action_id == ACTION_CATEGORY_SELECT:
            _handle_category_select(expense, store, action, response_url)
        else:
            logger.info(f"Unhandled action_id: {action_id}")
    except Exception as e:
        logger.exception(f"Interaction {action_id} failed: {e}")
        _ephemeral(response_url, "_Something went sideways — check the CloudWatch logs._")

    return _ok()


def _handle_file_it(
    expense: Expense, store: ExpenseStore, response_url: str, actor_slack_id: str
) -> None:
    """Commit the expense as filed. Clears needs_review.

    If the submitter hasn't picked a property yet, we reject the action
    with a soft nudge rather than filing an unallocated expense —
    unallocated rows would silently drop out of per-property exports.
    """
    if not expense.property_id:
        _ephemeral(
            response_url,
            "_Pick a property first — I can't file an expense without one._",
        )
        return

    expense.needs_review = False
    expense.review_reason = None
    expense.is_personal = False
    _sync_single_allocation(expense)
    store.put(expense)

    property_name = _property_display_name(expense.property_id)
    category_name = _category_display_name(expense.category_id)
    blocks, fallback = build_filed_card(
        expense=expense,
        property_display_name=property_name,
        category_display_name=category_name,
    )
    _replace_card(response_url, fallback, blocks)


def _handle_skip(
    expense: Expense, store: ExpenseStore, response_url: str, actor_slack_id: str
) -> None:
    """Mark the expense personal and exclude from Schedule E exports."""
    expense.is_personal = True
    expense.needs_review = False
    store.put(expense)

    blocks, fallback = build_skipped_card(expense=expense)
    _replace_card(response_url, fallback, blocks)


def _handle_split_placeholder(response_url: str) -> None:
    """Splits are v2 — tell the submitter and move on."""
    _ephemeral(
        response_url,
        (
            "_Splits across properties are coming in v2. For now, pick the "
            "primary property and note the split in the caption — we'll "
            "reconcile at tax time._"
        ),
    )


def _handle_property_select(
    expense: Expense, store: ExpenseStore, action: dict, response_url: str
) -> None:
    """Submitter picked a property from the dropdown. Update and re-render."""
    selected = _selected_option_value(action)
    if not selected:
        return
    # No-op if the user re-selected the same value Slack posted us.
    if expense.property_id == selected:
        return

    expense.property_id = selected
    _sync_single_allocation(expense)
    store.put(expense)

    _rerender_extracted_card(expense, response_url)


def _handle_category_select(
    expense: Expense, store: ExpenseStore, action: dict, response_url: str
) -> None:
    """Submitter picked a category. Clears the low-confidence flag so
    the red dot goes away on the re-rendered card."""
    selected = _selected_option_value(action)
    if not selected or selected not in valid_category_ids():
        return
    if expense.category_id == selected:
        return

    expense.category_id = selected
    expense.ocr_category_confidence = None  # user-confirmed, no more red dot
    store.put(expense)

    _rerender_extracted_card(expense, response_url)


# ─── Interaction helpers ─────────────────────────────────────────────────


def _rerender_extracted_card(expense: Expense, response_url: str) -> None:
    properties = TaskStore().load_properties()
    blocks, fallback = build_extracted_card(expense=expense, properties=properties)
    _replace_card(response_url, fallback, blocks)


def _sync_single_allocation(expense: Expense) -> None:
    """Keep the single-property allocation in step with property_id + total.

    MVP always runs at 100% against one property. Splits (multiple
    allocations summing to total) arrive in v2.
    """
    if not expense.property_id:
        expense.allocations = []
        return
    expense.allocations = [Allocation.single(expense.property_id, expense.total or "0.00")]


def _replace_card(response_url: str, text: str, blocks: list[dict]) -> None:
    if not response_url:
        return
    result = post_response_url(
        response_url,
        {
            "replace_original": True,
            "text": text,
            "blocks": blocks,
        },
    )
    if not result.get("ok"):
        logger.warning(f"response_url replace failed: {result}")


def _ephemeral(response_url: str, text: str) -> None:
    if not response_url:
        return
    post_response_url(
        response_url,
        {
            "response_type": "ephemeral",
            "replace_original": False,
            "text": text,
        },
    )


def _expense_id_from_action(action: dict) -> str:
    """Pull the expense id out of an interaction action.

    Buttons carry it as `value`. Dropdowns can't (their value slot is the
    selected option) so we embed the id into the `block_id` at render
    time via `expense_slack_ui.block_id_with_expense`.
    """
    value = action.get("value")
    if value:
        return value
    return expense_id_from_block_id(action.get("block_id", ""))


def _selected_option_value(action: dict) -> str:
    """For static_select actions, pull the chosen option's value."""
    selected = action.get("selected_option") or {}
    return selected.get("value", "")


def _property_display_name(property_id: Optional[str]) -> Optional[str]:
    if not property_id:
        return None
    for p in TaskStore().load_properties():
        if p.get("id") == property_id:
            return p.get("name") or property_id
    return property_id


def _category_display_name(category_id: Optional[str]) -> Optional[str]:
    if not category_id:
        return None
    row = get_category(category_id)
    if row:
        return row.get("display_name") or category_id
    return category_id


def _parse_form(raw_body: str) -> dict:
    """Parse an application/x-www-form-urlencoded body into a flat dict."""
    parsed = parse_qs(raw_body, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


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
