"""Daily cron Lambda: DM each user a digest of their overdue and due-today
tasks, and post a channel summary for anything more than 3 days overdue.

Runs once a day via EventBridge. Idempotent — safe to re-run.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from config import TASKS_CHANNEL_ID
from task_models import Task
from task_store import TaskStore
from task_slack_client import post_message, dm_user
from task_slack_ui import build_task_list_blocks


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# How many days overdue before a task also gets shouted into the channel.
_CHANNEL_ESCALATION_DAYS = 3


def lambda_handler(event, context):
    """Entry point for the daily escalation cron."""
    logger.info("Task escalator starting")

    store = TaskStore()
    today = datetime.now(timezone.utc).date().isoformat()
    tasks = store.list_all(include_closed=False)

    overdue_by_user: dict[str, list[Task]] = defaultdict(list)
    due_today_by_user: dict[str, list[Task]] = defaultdict(list)
    badly_overdue: list[Task] = []

    for t in tasks:
        if not t.due_date:
            continue
        if t.due_date < today:
            overdue_by_user[t.assignee_id].append(t)
            days_late = _days_between(t.due_date, today)
            if days_late > _CHANNEL_ESCALATION_DAYS:
                badly_overdue.append(t)
        elif t.due_date == today:
            due_today_by_user[t.assignee_id].append(t)

    stats = {
        "overdue_total": sum(len(v) for v in overdue_by_user.values()),
        "due_today_total": sum(len(v) for v in due_today_by_user.values()),
        "badly_overdue_total": len(badly_overdue),
        "dms_sent": 0,
        "channel_posts": 0,
    }

    properties = store.load_properties()
    users = store.load_users()

    # Per-user DM digest
    for user_id, overdue_list in overdue_by_user.items():
        user = store.get_user(user_id)
        if not user or not user.get("slack_user_id"):
            continue
        due_today_list = due_today_by_user.get(user_id, [])
        blocks = build_task_list_blocks(
            overdue_list + due_today_list,
            title=f"⏰ {len(overdue_list)} overdue, {len(due_today_list)} due today",
            empty_text="_All clear today._",
            properties=properties,
            users=users,
        )
        preview = f"You have {len(overdue_list)} overdue and {len(due_today_list)} due today."
        result = dm_user(user["slack_user_id"], preview, blocks=blocks)
        if result.get("ok"):
            stats["dms_sent"] += 1
        else:
            logger.warning(f"DM failed for {user_id}: {result.get('error')}")

    # Also DM users who only have due-today (no overdue) — we skipped them above
    for user_id, due_today_list in due_today_by_user.items():
        if user_id in overdue_by_user:
            continue
        user = store.get_user(user_id)
        if not user or not user.get("slack_user_id"):
            continue
        blocks = build_task_list_blocks(
            due_today_list,
            title=f"📅 {len(due_today_list)} due today",
            empty_text="_All clear today._",
            properties=properties,
            users=users,
        )
        preview = f"You have {len(due_today_list)} task(s) due today."
        result = dm_user(user["slack_user_id"], preview, blocks=blocks)
        if result.get("ok"):
            stats["dms_sent"] += 1

    # Channel summary for very-overdue tasks
    if badly_overdue and TASKS_CHANNEL_ID:
        blocks = build_task_list_blocks(
            badly_overdue,
            title=f"🚨 {len(badly_overdue)} task(s) overdue by more than {_CHANNEL_ESCALATION_DAYS} days",
            empty_text="_(none)_",
            properties=properties,
            users=users,
        )
        result = post_message(
            channel=TASKS_CHANNEL_ID,
            text=f"{len(badly_overdue)} task(s) very overdue",
            blocks=blocks,
        )
        if result.get("ok"):
            stats["channel_posts"] += 1

    logger.info(f"Escalator complete: {stats}")
    return {"statusCode": 200, "body": json.dumps(stats)}


def _days_between(earlier_iso_date: str, later_iso_date: str) -> int:
    """Days from earlier → later, assuming both are YYYY-MM-DD strings."""
    try:
        e = datetime.fromisoformat(earlier_iso_date).date()
        l = datetime.fromisoformat(later_iso_date).date()
        return (l - e).days
    except ValueError:
        return 0
