"""AWS Lambda handler for Slack interactions in the task-management workflow.

Routes three kinds of Slack traffic, all arriving over API Gateway:

  1. Slash commands  (POST /slack/tasks/commands, form-urlencoded)
     /task                 → open create modal
     /task mine            → list my open tasks
     /task list [property] → list open tasks for a property
     /task help            → brief cheatsheet in-channel

  2. Interactive components (POST /slack/tasks/interactions, form-urlencoded
     with a "payload" JSON field)
     - view_submission → create task from modal
     - block_actions   → button clicks (Mark done, Start, Block, Swap, Reopen)

  3. Events (POST /slack/tasks/events, JSON)
     - message events in a task thread → append as a comment

Signature verification reuses the same Slack v0 scheme as thread_handler.py.
Slash commands and interactive components are both form-urlencoded, so the
body parsing branches on content-type.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import parse_qs

from config import (
    TASKS_CHANNEL_ID,
    get_slack_signing_secret,
)
from task_models import (
    Task,
    Comment,
    STATUS_OPEN,
    STATUS_IN_PROGRESS,
    STATUS_BLOCKED,
    STATUS_DONE,
    PRIORITY_NORMAL,
    PROPERTY_BUSINESS,
    OPEN_STATUSES,
)
from task_store import TaskStore
from task_slack_client import (
    post_message,
    update_message,
    post_ephemeral,
    open_view,
    dm_user,
)
from task_slack_ui import (
    CREATE_MODAL_CALLBACK_ID,
    build_create_modal,
    build_task_card_blocks,
    build_card_fallback_text,
    build_task_list_blocks,
)


logger = logging.getLogger()
logger.setLevel(logging.INFO)

_MAX_TIMESTAMP_SKEW_SECONDS = 60 * 5

_HELP_TEXT = (
    "*Task commands*\n"
    "• `/task` — open the create form\n"
    "• `/task mine` — your open tasks\n"
    "• `/task list` — all open tasks\n"
    "• `/task list palm` — open tasks for a property (palm, villa, business)\n"
    "• `/task help` — this help\n\n"
    "Reply in a task's thread to leave a comment. Buttons on the card mark done, "
    "start, block, swap assignee, or reopen."
)


# ─── Entry point ─────────────────────────────────────────────────────────


def slack_tasks_handler(event, context):
    """API Gateway Lambda proxy entry point for all task Slack traffic."""
    raw_body = event.get("body", "")
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    headers = _lowercase_headers(event.get("headers", {}))

    # Fast-ack Slack retries without re-processing
    if headers.get("x-slack-retry-num"):
        logger.info(
            f"Slack retry #{headers['x-slack-retry-num']} "
            f"reason={headers.get('x-slack-retry-reason')} — acking only"
        )
        return _ok()

    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not _verify_signature(raw_body, timestamp, signature):
        logger.warning("Invalid Slack signature")
        return {"statusCode": 401, "body": "Invalid signature"}

    content_type = (headers.get("content-type") or "").lower()
    path = (event.get("rawPath") or event.get("path") or "").rstrip("/")

    try:
        # Events API always sends JSON; slash commands + interactions send
        # form-urlencoded.
        if "application/json" in content_type:
            payload = json.loads(raw_body)
            return _route_event(payload)

        form = _parse_form(raw_body)

        if "payload" in form:
            # Interactive components (block_actions, view_submission)
            payload = json.loads(form["payload"])
            return _route_interaction(payload)

        if "command" in form:
            return _route_slash_command(form)

        logger.info(f"Unrecognized request body on path={path}")
        return _ok()

    except Exception as e:
        # Never 500 to Slack — retries are expensive and we just log and
        # recover. The user may not see a reply, but the pipeline stays up.
        logger.exception(f"Error processing Slack task request: {e}")
        return _ok()


# ─── Slash commands ──────────────────────────────────────────────────────


def _route_slash_command(form: dict) -> dict:
    command = form.get("command", "").lower()
    text = (form.get("text", "") or "").strip()
    trigger_id = form.get("trigger_id", "")
    user_id = form.get("user_id", "")
    channel_id = form.get("channel_id", "")

    if command != "/task":
        logger.info(f"Ignoring unrecognized command: {command}")
        return _ok()

    args = text.split()
    subcommand = args[0].lower() if args else ""

    if subcommand in ("", "new", "create"):
        _open_create_modal(trigger_id=trigger_id)
        return _ok_ephemeral("Opening the new-task form…")

    if subcommand == "mine":
        return _list_mine_response(user_id=user_id, channel_id=channel_id)

    if subcommand == "list":
        property_arg = args[1].lower() if len(args) > 1 else ""
        return _list_response(property_arg=property_arg, channel_id=channel_id, user_id=user_id)

    if subcommand in ("help", "?"):
        return _ok_ephemeral(_HELP_TEXT)

    # Unknown — give the help text rather than silently swallowing.
    return _ok_ephemeral(
        f"Didn't recognize `{subcommand}`. Try:\n\n{_HELP_TEXT}"
    )


def _open_create_modal(*, trigger_id: str, prefilled_title: str = "", prefilled_description: str = "") -> None:
    store = TaskStore()
    view = build_create_modal(
        properties=store.load_properties(),
        users=store.load_users(),
        prefilled_title=prefilled_title,
        prefilled_description=prefilled_description,
    )
    result = open_view(trigger_id=trigger_id, view=view)
    if not result.get("ok"):
        logger.error(f"views.open failed: {result.get('error')}")


def _list_mine_response(*, user_id: str, channel_id: str) -> dict:
    store = TaskStore()
    user = store.get_user_by_slack_id(user_id)
    if not user:
        return _ok_ephemeral(
            "I don't have you in the users list yet. Ask VJ to add you to "
            "`config/users.json` in the tasks bucket."
        )
    tasks = store.list_by_assignee(user["id"], include_closed=False)
    blocks = build_task_list_blocks(
        tasks,
        title="📋 Your open tasks",
        empty_text="_Nothing on your plate. Nice._",
        properties=store.load_properties(),
        users=store.load_users(),
    )
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response_type": "ephemeral", "blocks": blocks}),
    }


def _list_response(*, property_arg: str, channel_id: str, user_id: str) -> dict:
    store = TaskStore()
    properties = store.load_properties()
    users = store.load_users()

    if not property_arg or property_arg in ("all", "*"):
        tasks = store.list_all(include_closed=False)
        title = "📋 All open tasks"
    elif property_arg in ("business", "biz", "bw"):
        tasks = [t for t in store.list_all(include_closed=False) if t.property_id == PROPERTY_BUSINESS]
        title = "📋 Open business-wide tasks"
    else:
        # Match by slug or name prefix (case-insensitive)
        match = _match_property(property_arg, properties)
        if not match:
            return _ok_ephemeral(
                f"I don't know a property called `{property_arg}`. "
                f"Known: {', '.join(p.get('slug') or p['id'] for p in properties)}, business."
            )
        tasks = [t for t in store.list_all(include_closed=False) if t.property_id == match["id"]]
        title = f"📋 Open tasks · {match.get('name', match['id'])}"

    blocks = build_task_list_blocks(
        tasks,
        title=title,
        empty_text="_Nothing open here._",
        properties=properties,
        users=users,
    )
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response_type": "ephemeral", "blocks": blocks}),
    }


def _match_property(arg: str, properties: list[dict]) -> Optional[dict]:
    arg = arg.lower()
    for p in properties:
        slug = (p.get("slug") or "").lower()
        name = (p.get("name") or "").lower()
        if slug == arg or name == arg:
            return p
    # Prefix match on name or slug
    for p in properties:
        slug = (p.get("slug") or "").lower()
        name = (p.get("name") or "").lower()
        if slug.startswith(arg) or name.startswith(arg):
            return p
    return None


# ─── Interactive components ──────────────────────────────────────────────


def _route_interaction(payload: dict) -> dict:
    kind = payload.get("type")

    if kind == "view_submission":
        return _handle_view_submission(payload)

    if kind == "block_actions":
        _handle_block_actions(payload)
        # Slack just needs a 200 for block_actions; the UI update happens
        # asynchronously via chat.update or chat.postMessage.
        return _ok()

    logger.info(f"Ignoring interaction type: {kind}")
    return _ok()


def _handle_view_submission(payload: dict) -> dict:
    view = payload.get("view", {}) or {}
    callback_id = view.get("callback_id")
    if callback_id != CREATE_MODAL_CALLBACK_ID:
        logger.info(f"Ignoring view_submission callback_id={callback_id}")
        return _ok()

    values = view.get("state", {}).get("values", {}) or {}
    user_slack_id = payload.get("user", {}).get("id", "")

    title = _get_input(values, "title_block", "title")
    description = _get_input(values, "description_block", "description")
    property_id = _get_select(values, "property_block", "property") or PROPERTY_BUSINESS
    assignee_value = _get_select(values, "assignee_block", "assignee")
    priority = _get_select(values, "priority_block", "priority") or PRIORITY_NORMAL
    due_date = _get_date(values, "due_date_block", "due_date")

    errors: dict = {}
    if not title or not title.strip():
        errors["title_block"] = "Give the task a title."
    if not assignee_value:
        errors["assignee_block"] = "Pick an assignee."

    if errors:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"response_action": "errors", "errors": errors}),
        }

    store = TaskStore()
    creator = store.get_user_by_slack_id(user_slack_id)
    if not creator:
        # Rare, but surface it clearly instead of failing silently.
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "response_action": "errors",
                    "errors": {
                        "title_block": (
                            "I don't have you in the users list yet — "
                            "ask VJ to add your Slack id."
                        )
                    },
                }
            ),
        }

    task = Task.new(
        title=title,
        description=description or "",
        property_id=property_id,
        assignee_id=assignee_value,
        created_by_id=creator["id"],
        priority=priority,
        due_date=due_date,
    )

    _create_and_announce(task, store)
    return _ok()


def _create_and_announce(task: Task, store: TaskStore) -> None:
    """Persist a task, post its card to #tasks, store the thread pointer,
    and DM the assignee.
    """
    if not TASKS_CHANNEL_ID:
        logger.error("TASKS_CHANNEL_ID not configured — task created but not posted")
        store.put(task)
        return

    properties = store.load_properties()
    users = store.load_users()
    blocks = build_task_card_blocks(task, properties=properties, users=users)
    fallback = build_card_fallback_text(task, properties, users)

    result = post_message(channel=TASKS_CHANNEL_ID, text=fallback, blocks=blocks)
    if result.get("ok"):
        task.slack_channel_id = result.get("channel") or TASKS_CHANNEL_ID
        task.slack_message_ts = result.get("ts")
        store.put(task)
        if task.slack_message_ts:
            store.put_slack_index(task.slack_message_ts, task.id)
    else:
        # Save the task anyway so work isn't lost; log loudly.
        logger.error(f"Failed to post task card: {result.get('error')}")
        store.put(task)

    # DM the assignee, unless they created it for themselves.
    assignee = store.get_user(task.assignee_id)
    if assignee and assignee.get("slack_user_id") and assignee["id"] != task.created_by_id:
        creator = store.get_user(task.created_by_id) or {}
        creator_name = creator.get("display_name") or "someone"
        preamble = (
            f"{creator_name} assigned you a task: *{task.title}*"
        )
        dm_user(assignee["slack_user_id"], preamble, blocks=blocks)


def _handle_block_actions(payload: dict) -> None:
    actions = payload.get("actions", []) or []
    if not actions:
        return

    action = actions[0]
    action_id = action.get("action_id", "")
    task_id = action.get("value", "")
    user_slack_id = payload.get("user", {}).get("id", "")
    response_url = payload.get("response_url")  # used for ephemeral messages

    store = TaskStore()
    task = store.get(task_id)
    if not task:
        logger.warning(f"Action {action_id} for unknown task {task_id}")
        _ephemeral_reply(response_url, f"_That task no longer exists (id {task_id[:8]})._")
        return

    actor = store.get_user_by_slack_id(user_slack_id)
    actor_name = (actor or {}).get("display_name") or "someone"

    if action_id == "task_mark_done":
        _close_task(task, store, actor_name)
    elif action_id == "task_reopen":
        _reopen_task(task, store, actor_name)
    elif action_id == "task_start":
        _set_status(task, store, STATUS_IN_PROGRESS, actor_name, verb="started")
    elif action_id == "task_unstart":
        _set_status(task, store, STATUS_OPEN, actor_name, verb="paused")
    elif action_id == "task_block":
        _set_status(task, store, STATUS_BLOCKED, actor_name, verb="blocked")
    elif action_id == "task_unblock":
        _set_status(task, store, STATUS_OPEN, actor_name, verb="unblocked")
    elif action_id == "task_swap_assignee":
        _swap_assignee(task, store, actor_name)
    else:
        logger.info(f"Unhandled action_id: {action_id}")


def _close_task(task: Task, store: TaskStore, actor_name: str) -> None:
    task.status = STATUS_DONE
    task.completed_at = _now_iso()
    store.put(task)
    _refresh_card(task, store)
    _thread_reply(task, f"✅ Closed by {actor_name} at {_format_time(task.completed_at)}.")


def _reopen_task(task: Task, store: TaskStore, actor_name: str) -> None:
    task.status = STATUS_OPEN
    task.completed_at = None
    store.put(task)
    _refresh_card(task, store)
    _thread_reply(task, f"↩️ Reopened by {actor_name}.")


def _set_status(task: Task, store: TaskStore, new_status: str, actor_name: str, *, verb: str) -> None:
    task.status = new_status
    store.put(task)
    _refresh_card(task, store)
    _thread_reply(task, f"🔄 {actor_name} {verb} this task.")


def _swap_assignee(task: Task, store: TaskStore, actor_name: str) -> None:
    users = store.load_users()
    if len(users) != 2:
        # Swap only makes sense with exactly two; otherwise no-op with notice.
        _thread_reply(task, "_Swap only works with exactly two users configured._")
        return
    other = next((u for u in users if u["id"] != task.assignee_id), None)
    if not other:
        _thread_reply(task, "_Couldn't find the other user to swap to._")
        return

    previous_assignee = store.get_user(task.assignee_id) or {}
    task.assignee_id = other["id"]
    store.put(task)
    _refresh_card(task, store)

    prev_name = previous_assignee.get("display_name") or "previous assignee"
    new_name = other.get("display_name") or other["id"]
    _thread_reply(task, f"🔁 {actor_name} reassigned from {prev_name} → {new_name}.")

    # DM the new assignee
    if other.get("slack_user_id"):
        dm_user(
            other["slack_user_id"],
            f"{actor_name} reassigned a task to you: *{task.title}*",
            blocks=build_task_card_blocks(
                task, properties=store.load_properties(), users=store.load_users()
            ),
        )


def _refresh_card(task: Task, store: TaskStore) -> None:
    """Re-render the task's Slack card in place."""
    if not (task.slack_channel_id and task.slack_message_ts):
        return
    properties = store.load_properties()
    users = store.load_users()
    blocks = build_task_card_blocks(task, properties=properties, users=users)
    fallback = build_card_fallback_text(task, properties, users)
    result = update_message(
        channel=task.slack_channel_id,
        ts=task.slack_message_ts,
        text=fallback,
        blocks=blocks,
    )
    if not result.get("ok"):
        logger.warning(f"chat.update failed for task {task.id}: {result.get('error')}")


def _thread_reply(task: Task, text: str) -> None:
    if not (task.slack_channel_id and task.slack_message_ts):
        return
    post_message(
        channel=task.slack_channel_id,
        text=text,
        thread_ts=task.slack_message_ts,
    )


# ─── Events API: thread replies become comments ──────────────────────────


def _route_event(payload: dict) -> dict:
    ptype = payload.get("type")
    if ptype == "url_verification":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/plain"},
            "body": payload.get("challenge", ""),
        }
    if ptype != "event_callback":
        return _ok()

    inner = payload.get("event", {}) or {}
    if inner.get("type") != "message":
        return _ok()
    subtype = inner.get("subtype")
    if subtype and subtype not in ("file_share",):
        return _ok()
    if inner.get("bot_id") or inner.get("app_id"):
        return _ok()

    thread_ts = inner.get("thread_ts")
    ts = inner.get("ts")
    if not thread_ts or thread_ts == ts:
        return _ok()

    channel = inner.get("channel", "")
    if TASKS_CHANNEL_ID and channel != TASKS_CHANNEL_ID:
        return _ok()

    store = TaskStore()
    task = store.get_task_by_thread(thread_ts)
    if not task:
        # Could be a guest-alert thread in the same channel (shouldn't happen
        # if channels are separate, but be defensive).
        return _ok()

    text = (inner.get("text") or "").strip()
    if not text:
        return _ok()

    user_slack_id = inner.get("user", "")
    user = store.get_user_by_slack_id(user_slack_id)
    user_id = user["id"] if user else user_slack_id

    task.comments.append(Comment.new(user_id=user_id, body=text, slack_message_ts=ts))
    store.put(task)
    return _ok()


# ─── Signing + helpers ───────────────────────────────────────────────────


def _verify_signature(raw_body: str, timestamp: str, signature: str) -> bool:
    secret = get_slack_signing_secret()
    if not secret:
        logger.error("Slack signing secret not configured — rejecting request")
        return False
    if not timestamp or not signature:
        return False
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > _MAX_TIMESTAMP_SKEW_SECONDS:
        return False
    basestring = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    computed = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def _lowercase_headers(headers: dict) -> dict:
    return {k.lower(): v for k, v in (headers or {}).items()}


def _parse_form(raw_body: str) -> dict:
    """Slack sends form-urlencoded bodies for slash commands + interactions.

    parse_qs returns lists; Slack fields are always singular, so we flatten.
    """
    parsed = parse_qs(raw_body, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def _get_input(values: dict, block_id: str, action_id: str) -> str:
    return (values.get(block_id, {}).get(action_id, {}).get("value") or "").strip()


def _get_select(values: dict, block_id: str, action_id: str) -> str:
    return (
        values.get(block_id, {})
        .get(action_id, {})
        .get("selected_option", {})
        .get("value")
        or ""
    )


def _get_date(values: dict, block_id: str, action_id: str) -> str:
    return values.get(block_id, {}).get(action_id, {}).get("selected_date") or ""


def _ok() -> dict:
    return {"statusCode": 200, "body": ""}


def _ok_ephemeral(text: str) -> dict:
    """Respond to a slash command with an ephemeral message."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response_type": "ephemeral", "text": text}),
    }


def _ephemeral_reply(response_url: Optional[str], text: str) -> None:
    """Post to a Slack response_url. Used when we're past the initial ack."""
    if not response_url:
        return
    try:
        import requests

        requests.post(
            response_url,
            json={"response_type": "ephemeral", "text": text, "replace_original": False},
            timeout=3,
        )
    except Exception as e:
        logger.warning(f"response_url post failed: {e}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_time(iso: str) -> str:
    """Format an ISO timestamp for human display in thread replies."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso
