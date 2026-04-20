"""Block Kit builders for task modals, cards, and list views.

All Slack rendering lives here so handlers stay focused on flow. Functions
are pure: they take task/context data and return Slack block dicts.
"""

from datetime import datetime, timezone
from typing import Optional

from task_models import (
    Task,
    STATUS_LABEL,
    PRIORITY_LABEL,
    PRIORITY_EMOJI,
    ALL_PRIORITIES,
    ALL_STATUSES,
    OPEN_STATUSES,
    STATUS_OPEN,
    STATUS_IN_PROGRESS,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_CANCELLED,
    PRIORITY_NORMAL,
    PROPERTY_BUSINESS,
)


_MAX_TEXT_LEN = 3000


def _truncate(text: str, limit: int = _MAX_TEXT_LEN) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# ─── Modal: create/edit task ─────────────────────────────────────────────

CREATE_MODAL_CALLBACK_ID = "task_create_submit"


def build_create_modal(
    *,
    properties: list[dict],
    users: list[dict],
    prefilled_title: str = "",
    prefilled_description: str = "",
) -> dict:
    """Return the full views.open payload for the task-creation modal."""
    property_options = _property_options(properties)
    user_options = _user_options(users)
    priority_options = [
        {
            "text": {"type": "plain_text", "text": f"{PRIORITY_EMOJI[p]} {PRIORITY_LABEL[p]}"},
            "value": p,
        }
        for p in ALL_PRIORITIES
    ]

    blocks = [
        {
            "type": "input",
            "block_id": "title_block",
            "label": {"type": "plain_text", "text": "Title"},
            "element": {
                "type": "plain_text_input",
                "action_id": "title",
                "initial_value": prefilled_title[:150] if prefilled_title else "",
                "max_length": 200,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Fix garbage disposal at Palm",
                },
            },
        },
        {
            "type": "input",
            "block_id": "description_block",
            "label": {"type": "plain_text", "text": "Details"},
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "description",
                "initial_value": prefilled_description[:2000] if prefilled_description else "",
                "multiline": True,
                "max_length": 3000,
            },
        },
        {
            "type": "input",
            "block_id": "property_block",
            "label": {"type": "plain_text", "text": "Property"},
            "element": {
                "type": "static_select",
                "action_id": "property",
                "options": property_options,
                "initial_option": property_options[0] if property_options else None,
            },
        },
        {
            "type": "input",
            "block_id": "assignee_block",
            "label": {"type": "plain_text", "text": "Assignee"},
            "element": {
                "type": "static_select",
                "action_id": "assignee",
                "options": user_options,
                "initial_option": user_options[0] if user_options else None,
            },
        },
        {
            "type": "input",
            "block_id": "priority_block",
            "label": {"type": "plain_text", "text": "Priority"},
            "element": {
                "type": "static_select",
                "action_id": "priority",
                "options": priority_options,
                "initial_option": next(
                    (o for o in priority_options if o["value"] == PRIORITY_NORMAL),
                    priority_options[1] if len(priority_options) > 1 else priority_options[0],
                ),
            },
        },
        {
            "type": "input",
            "block_id": "due_date_block",
            "label": {"type": "plain_text", "text": "Due date"},
            "optional": True,
            "element": {
                "type": "datepicker",
                "action_id": "due_date",
            },
        },
    ]

    # Strip None initial_option entries (Slack rejects them)
    for block in blocks:
        element = block.get("element", {})
        if element.get("initial_option") is None:
            element.pop("initial_option", None)

    return {
        "type": "modal",
        "callback_id": CREATE_MODAL_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "New task"},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _property_options(properties: list[dict]) -> list[dict]:
    """Dropdown options for property selection. Always includes Business-wide."""
    options = []
    for p in properties:
        name = p.get("name") or p.get("slug") or p["id"]
        options.append(
            {
                "text": {"type": "plain_text", "text": name},
                "value": p["id"],
            }
        )
    options.append(
        {
            "text": {"type": "plain_text", "text": "🏢 Business-wide"},
            "value": PROPERTY_BUSINESS,
        }
    )
    return options


def _user_options(users: list[dict]) -> list[dict]:
    """Dropdown options for assignee selection."""
    options = []
    for u in users:
        name = u.get("display_name") or u.get("slack_user_id") or u["id"]
        options.append(
            {
                "text": {"type": "plain_text", "text": name},
                "value": u["id"],
            }
        )
    return options


# ─── Task card: the main view of a task ──────────────────────────────────


def build_task_card_blocks(
    task: Task,
    *,
    properties: list[dict],
    users: list[dict],
) -> list[dict]:
    """Block Kit for the task's card in #tasks. Used on create and update."""
    property_name = _lookup_property_name(task.property_id, properties)
    assignee_name = _lookup_user_name(task.assignee_id, users)
    creator_name = _lookup_user_name(task.created_by_id, users)

    priority_emoji = PRIORITY_EMOJI.get(task.priority, "🟢")
    status_label = STATUS_LABEL.get(task.status, task.status)

    # Header: priority + title. Keep it short and scannable.
    header_text = _truncate(f"{priority_emoji} {task.title}", limit=150)

    fields = [
        {"type": "mrkdwn", "text": f"*Property*\n{property_name}"},
        {"type": "mrkdwn", "text": f"*Assignee*\n{assignee_name}"},
        {"type": "mrkdwn", "text": f"*Status*\n{status_label}"},
        {"type": "mrkdwn", "text": f"*Priority*\n{PRIORITY_LABEL.get(task.priority, task.priority)}"},
    ]
    if task.due_date:
        due_label = _format_due_date(task.due_date, task.status)
        fields.append({"type": "mrkdwn", "text": f"*Due*\n{due_label}"})
    fields.append({"type": "mrkdwn", "text": f"*Created by*\n{creator_name}"})

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {"type": "section", "fields": fields},
    ]

    if task.description:
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _truncate(task.description)},
                },
            ]
        )

    if task.tags:
        tag_text = "  ".join(f"`{t}`" for t in task.tags)
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": tag_text}],
            }
        )

    # Action buttons. Closed tasks get a Reopen-only view.
    blocks.append({"type": "divider"})
    blocks.append(_action_buttons(task))

    # Footer: task id + created timestamp for reference
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"`{task.id[:8]}` · created {_format_iso_short(task.created_at)}",
                }
            ],
        }
    )

    return blocks


def _action_buttons(task: Task) -> dict:
    """The row of buttons under the card. Value includes the task id."""
    tid = task.id
    elements: list[dict] = []

    if task.status in OPEN_STATUSES:
        # Mark done
        elements.append(
            {
                "type": "button",
                "action_id": "task_mark_done",
                "text": {"type": "plain_text", "text": "✅ Mark done"},
                "value": tid,
                "style": "primary",
            }
        )
        # Start / un-start toggle
        if task.status == STATUS_OPEN:
            elements.append(
                {
                    "type": "button",
                    "action_id": "task_start",
                    "text": {"type": "plain_text", "text": "▶️ Start"},
                    "value": tid,
                }
            )
        elif task.status == STATUS_IN_PROGRESS:
            elements.append(
                {
                    "type": "button",
                    "action_id": "task_unstart",
                    "text": {"type": "plain_text", "text": "⏸ Pause"},
                    "value": tid,
                }
            )
        # Block / unblock
        if task.status != STATUS_BLOCKED:
            elements.append(
                {
                    "type": "button",
                    "action_id": "task_block",
                    "text": {"type": "plain_text", "text": "🚧 Block"},
                    "value": tid,
                }
            )
        else:
            elements.append(
                {
                    "type": "button",
                    "action_id": "task_unblock",
                    "text": {"type": "plain_text", "text": "🔓 Unblock"},
                    "value": tid,
                }
            )
        # Swap assignee (only two users — single click flips it)
        elements.append(
            {
                "type": "button",
                "action_id": "task_swap_assignee",
                "text": {"type": "plain_text", "text": "🔁 Swap assignee"},
                "value": tid,
            }
        )
    else:
        # Closed — offer reopen only
        elements.append(
            {
                "type": "button",
                "action_id": "task_reopen",
                "text": {"type": "plain_text", "text": "↩️ Reopen"},
                "value": tid,
            }
        )

    return {"type": "actions", "block_id": "task_actions", "elements": elements}


def build_card_fallback_text(task: Task, properties: list[dict], users: list[dict]) -> str:
    """Plain text used for mobile push notifications + Slack's text fallback."""
    property_name = _lookup_property_name(task.property_id, properties)
    assignee_name = _lookup_user_name(task.assignee_id, users)
    emoji = PRIORITY_EMOJI.get(task.priority, "🟢")
    return f"{emoji} {task.title} — {property_name} · {assignee_name}"


# ─── List view: "my open tasks", "all open for Palm", etc. ───────────────


def build_task_list_blocks(
    tasks: list[Task],
    *,
    title: str,
    empty_text: str,
    properties: list[dict],
    users: list[dict],
) -> list[dict]:
    """Compact multi-task list rendered as a single ephemeral reply."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title, "emoji": True},
        }
    ]
    if not tasks:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": empty_text}}
        )
        return blocks

    # Sort: overdue first, then priority (urgent→low), then due date, then created
    priority_rank = {p: i for i, p in enumerate(("urgent", "high", "normal", "low"))}
    today = datetime.now(timezone.utc).date().isoformat()

    def sort_key(t: Task):
        overdue = 0 if t.is_overdue(today) else 1
        return (
            overdue,
            priority_rank.get(t.priority, 99),
            t.due_date or "9999-12-31",
            t.created_at,
        )

    tasks = sorted(tasks, key=sort_key)

    for t in tasks:
        blocks.append(_list_row(t, properties, users))

    return blocks


def _list_row(task: Task, properties: list[dict], users: list[dict]) -> dict:
    """One task rendered as a section block with a Mark-done accessory."""
    property_name = _lookup_property_name(task.property_id, properties)
    assignee_name = _lookup_user_name(task.assignee_id, users)
    emoji = PRIORITY_EMOJI.get(task.priority, "🟢")
    due = ""
    if task.due_date:
        due = f" · due {_format_due_date(task.due_date, task.status)}"
    line = f"{emoji} *{task.title}*\n_{property_name} · {assignee_name}{due}_"

    block = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": _truncate(line, limit=2800)},
    }
    if task.status in OPEN_STATUSES:
        block["accessory"] = {
            "type": "button",
            "action_id": "task_mark_done",
            "text": {"type": "plain_text", "text": "Done"},
            "value": task.id,
            "style": "primary",
        }
    return block


# ─── Helpers ─────────────────────────────────────────────────────────────


def _lookup_property_name(property_id: str, properties: list[dict]) -> str:
    if property_id == PROPERTY_BUSINESS:
        return "🏢 Business-wide"
    for p in properties:
        if p.get("id") == property_id:
            return p.get("name") or p.get("slug") or property_id
    return property_id or "(unknown property)"


def _lookup_user_name(user_id: str, users: list[dict]) -> str:
    for u in users:
        if u.get("id") == user_id:
            return u.get("display_name") or u.get("slack_user_id") or user_id
    return user_id or "(unknown user)"


def _format_due_date(due_date: str, status: str) -> str:
    """Render a due date as relative text for humans."""
    if not due_date:
        return ""
    try:
        d = datetime.fromisoformat(due_date).date()
    except ValueError:
        return due_date
    today = datetime.now(timezone.utc).date()
    delta = (d - today).days
    if status not in OPEN_STATUSES:
        return due_date
    if delta < 0:
        return f"{due_date} ⚠️ overdue by {-delta}d"
    if delta == 0:
        return f"{due_date} (today)"
    if delta == 1:
        return f"{due_date} (tomorrow)"
    if delta <= 7:
        return f"{due_date} (in {delta}d)"
    return due_date


def _format_iso_short(iso: str) -> str:
    """Strip an ISO timestamp to YYYY-MM-DD for display."""
    if not iso:
        return ""
    return iso.split("T", 1)[0]
