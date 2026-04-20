"""Natural-language parser for the `/task …` one-liner quick-create flow.

Design: the vocabulary is tiny (2 users, 2 properties, 4 priorities, a
handful of relative dates), so deterministic token matching beats calling
Claude. We scan from the *front* and from the *back* of the token list —
metadata tokens in the middle of the sentence are left alone so the title
keeps its natural wording.

Example: `maggie rearrange photos for villa urgent tomorrow`
  front: maggie → assignee
  back:  tomorrow → due, urgent → priority, villa → property
  title: "rearrange photos for" → trailing "for" stripped → "rearrange photos"

False-positive avoidance: we only consume tokens at the boundaries, so a
title like "follow up with low-rent listing" keeps "low" in place.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from task_models import (
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_HIGH,
    PRIORITY_URGENT,
    PROPERTY_BUSINESS,
)


_CONNECTIVES = {"for", "at", "in", "on", "by", "to", "-", "—"}

_PRIORITY_LOOKUP = {
    "low": PRIORITY_LOW,
    "normal": PRIORITY_NORMAL,
    "medium": PRIORITY_NORMAL,
    "med": PRIORITY_NORMAL,
    "high": PRIORITY_HIGH,
    "urgent": PRIORITY_URGENT,
}

_DAY_OF_WEEK = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def parse_quick_create(
    text: str,
    *,
    users: list[dict],
    properties: list[dict],
) -> dict:
    """Return {assignee_id, property_id, priority, due_date, title}.

    Any field we couldn't parse is None (caller supplies defaults). Title
    is always a string — possibly empty if the user typed only metadata.
    """
    user_lookup = _build_user_lookup(users)
    prop_lookup = _build_property_lookup(properties)

    tokens = text.split()

    result: dict = {
        "assignee_id": None,
        "property_id": None,
        "priority": None,
        "due_date": None,
        "title": "",
    }

    # Front: consume known metadata tokens from the start
    head = 0
    while head < len(tokens):
        matched = _match_token(tokens[head], result, user_lookup, prop_lookup)
        if not matched:
            break
        head += 1

    # Back: consume known metadata tokens from the end
    tail = len(tokens)
    while tail > head:
        matched = _match_token(tokens[tail - 1], result, user_lookup, prop_lookup)
        if not matched:
            break
        tail -= 1

    # The middle is the title. Clean up trailing connectives ("for", "at",
    # etc.) that lost their object when we stripped a property/assignee.
    title_tokens = list(tokens[head:tail])
    while title_tokens and title_tokens[-1].lower().strip(",.!?") in _CONNECTIVES:
        title_tokens.pop()
    while title_tokens and title_tokens[0].lower().strip(",.!?") in _CONNECTIVES:
        title_tokens.pop(0)

    result["title"] = " ".join(title_tokens).strip()
    return result


def _match_token(
    token: str,
    result: dict,
    user_lookup: dict,
    prop_lookup: dict,
) -> bool:
    """Try to attribute `token` to an unset field. Returns True if consumed."""
    key = token.lower().strip(",.!?")

    if result["assignee_id"] is None and key in user_lookup:
        result["assignee_id"] = user_lookup[key]
        return True
    if result["property_id"] is None and key in prop_lookup:
        result["property_id"] = prop_lookup[key]
        return True
    if result["priority"] is None and key in _PRIORITY_LOOKUP:
        result["priority"] = _PRIORITY_LOOKUP[key]
        return True
    if result["due_date"] is None:
        due = _parse_due(key)
        if due:
            result["due_date"] = due
            return True
    return False


def _build_user_lookup(users: list[dict]) -> dict:
    """Accept either `id` (e.g. "vj") or `display_name` (e.g. "VJ")."""
    lookup: dict = {}
    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        if u.get("id"):
            lookup[u["id"].lower()] = uid
        if u.get("display_name"):
            lookup[u["display_name"].lower()] = uid
    return lookup


def _build_property_lookup(properties: list[dict]) -> dict:
    """Accept slug, name (single-word only to avoid greedy matches), or
    the fixed aliases for business-wide tasks.
    """
    lookup: dict = {
        "business": PROPERTY_BUSINESS,
        "biz": PROPERTY_BUSINESS,
        "bw": PROPERTY_BUSINESS,
    }
    for p in properties:
        pid = p.get("id")
        if not pid:
            continue
        if p.get("slug"):
            lookup[p["slug"].lower()] = pid
        # Only index single-word names to avoid capturing multi-word titles.
        # "palm" and "villa" both have multi-word official names ("The Palm
        # Club", "Villa Bougainvillea") — the slug path already covers them.
        name = p.get("name") or ""
        if name and " " not in name:
            lookup[name.lower()] = pid
    return lookup


def _parse_due(token: str) -> Optional[str]:
    """Parse a single token as a date. Returns ISO YYYY-MM-DD or None.

    Handles: `today`, `tomorrow`/`tmrw`, ISO `YYYY-MM-DD`, and day-of-week
    names (which resolve to the *next* occurrence, skipping today).
    """
    today = datetime.now(timezone.utc).date()

    if token == "today":
        return today.isoformat()
    if token in ("tomorrow", "tmrw", "tom"):
        return (today + timedelta(days=1)).isoformat()

    # ISO date
    if len(token) == 10 and token[4] == "-" and token[7] == "-":
        try:
            datetime.fromisoformat(token)
            return token
        except ValueError:
            return None

    # Day-of-week → next occurrence (never today)
    if token in _DAY_OF_WEEK:
        target = _DAY_OF_WEEK[token]
        current = today.weekday()
        delta = (target - current) % 7
        if delta == 0:
            delta = 7
        return (today + timedelta(days=delta)).isoformat()

    return None
