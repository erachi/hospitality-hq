"""Ambient Claude agent for per-stay Slack channels.

Every top-level message in a stay channel — whether it's a mirrored
Hospitable guest message or a human asking a question — triggers the
agent. The agent reads the full stay journal plus the property KB and
thread logs, chats multi-turn with VJ and Maggie, and optionally
proposes a guest-visible draft wrapped in `<<DRAFT>>...<<END_DRAFT>>`
tags that we turn into a Block Kit card with Send/Discard buttons.

Hard invariants:
- The agent NEVER sends a message to a guest on its own. Only a human
  clicking "Send to guest" on a draft card actually calls Hospitable.
- S3 journal writes happen BEFORE Slack writes, both for the incoming
  human turn and for the agent's own response. If the Claude call or
  Slack post dies, the conversation record is intact.
- `conversations.history` is never consulted. On free Slack it will
  come back empty past ~90 days, so the journal is the only reliable
  transcript.

Also houses the draft-card button handlers (`stay_send_draft`,
`stay_discard_draft`) since they operate on the same stay journal and
Slack channel the agent writes to. Edit is deferred to a later PR.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import anthropic

from classifier import _get_client as _get_anthropic_client
from config import DRAFT_MODEL, get_slack_bot_token
from hospitable_client import HospitableClient
from knowledge_base_loader import load_kb, format_for_claude as format_kb
from slack_notifier import StayChannelSlackClient
from stay_channels import StayChannels
from stay_models import (
    SOURCE_HOSPITABLE_GUEST,
    SOURCE_HOSPITABLE_HOST,
    SOURCE_SLACK_AGENT,
    SOURCE_SLACK_USER,
    SOURCE_SYSTEM,
    StayEntry,
)
from stay_store import StayStore
from thread_logs import ThreadLogs, format_for_claude as format_logs

logger = logging.getLogger(__name__)


# Last N journal entries given to the agent as conversation context.
# Older entries live in the journal and are summarized in-system-prompt;
# ~50 keeps token cost sane while covering most live-stay chatter.
_RECENT_ENTRIES_WINDOW = 50

# How many tokens to let Claude spend on one turn. Drafts are short
# (~150 tokens) but chat can be longer when the agent explains
# reasoning or lists options.
_MAX_TOKENS = 800

# Draft fencing — must match what the system prompt instructs the
# model to emit. Case-sensitive.
_DRAFT_OPEN = "<<DRAFT>>"
_DRAFT_CLOSE = "<<END_DRAFT>>"


# ─── Public entry points ───────────────────────────────────────────────


def handle_channel_message(
    inner_event: dict,
    stay_row: dict,
    *,
    slack_client: Optional[StayChannelSlackClient] = None,
    stay_channels: Optional[StayChannels] = None,
    stay_store: Optional[StayStore] = None,
) -> None:
    """Dispatch a stay-channel top-level message to the agent.

    Called by `thread_handler.slack_events_handler` after it has
    classified the event as a top-level message in an active stay
    channel.

    All downstream errors are logged and swallowed — the Slack event
    retry budget is tight and we never want a Claude or Hospitable
    hiccup to burn through it.
    """
    try:
        _handle_channel_message(
            inner_event,
            stay_row,
            slack_client=slack_client or StayChannelSlackClient(),
            stay_channels=stay_channels or StayChannels(),
            stay_store=stay_store or StayStore(),
        )
    except Exception as e:
        logger.exception("stay_agent: unhandled error: %s", e)


def _handle_channel_message(
    inner_event: dict,
    stay_row: dict,
    *,
    slack_client: StayChannelSlackClient,
    stay_channels: StayChannels,
    stay_store: StayStore,
) -> None:
    reservation_uuid = stay_row.get("reservation_uuid", "")
    channel_id = stay_row.get("channel_id", "")
    if not reservation_uuid or not channel_id:
        logger.warning("stay_agent: malformed stay_row: %s", stay_row)
        return

    text = (inner_event.get("text") or "").strip()
    if not text:
        return

    user_id = inner_event.get("user", "unknown")
    slack_ts = inner_event.get("ts", "")

    # ── 1. Journal first: persist the human turn BEFORE calling Claude.
    #       Even if Claude or Slack fails, the record of what VJ or
    #       Maggie said stays in S3.
    user_entry = StayEntry.new(
        source=SOURCE_SLACK_USER,
        author=user_id,
        body=text,
        metadata={"slack_ts": slack_ts, "slack_channel_id": channel_id},
    )
    try:
        stay_store.append_entry(reservation_uuid, user_entry)
    except Exception as e:
        logger.error(
            "stay_agent: failed to append user turn for %s: %s",
            reservation_uuid,
            e,
        )
        # Keep going — losing the journal row is bad but the agent can
        # still produce something useful from the fresh text + prior
        # journal state.

    # ── 2. Load reservation context from durable sources only.
    journal = stay_store.get(reservation_uuid)
    reservation_context = _load_reservation_context(stay_row)
    kb_text = _load_kb_text(stay_row.get("property_id", ""))
    thread_logs_text = _load_thread_logs_text(reservation_uuid)

    # ── 3. Build the multi-turn messages for Claude.
    messages = _build_turns(
        journal=journal,
        trigger_text=text,
        trigger_user_id=user_id,
    )
    system_prompt = _build_system_prompt(
        reservation_context=reservation_context,
        kb_text=kb_text,
        thread_logs_text=thread_logs_text,
    )

    # ── 4. Call Claude. Swallow errors with a user-facing apology.
    try:
        response_text = _call_claude(system_prompt, messages)
    except Exception as e:
        logger.exception("stay_agent: Claude call failed: %s", e)
        try:
            slack_client.post_message(
                channel_id,
                text=":warning: I hit an error thinking that through. Try again?",
            )
        except Exception:
            pass
        return

    # ── 5. Split chat from draft and persist the agent turn BEFORE
    #       posting to Slack.
    chat_text, draft_text = _extract_draft(response_text)
    agent_entry = StayEntry.new(
        source=SOURCE_SLACK_AGENT,
        author="agent",
        body=response_text,  # full model output incl. draft tags
        metadata={
            "has_draft": bool(draft_text),
            "draft_text": draft_text or "",
            "model": DRAFT_MODEL,
        },
    )
    try:
        stay_store.append_entry(reservation_uuid, agent_entry)
    except Exception as e:
        logger.warning(
            "stay_agent: failed to append agent turn for %s: %s",
            reservation_uuid,
            e,
        )

    # ── 6. Post chat to Slack. The chat portion can be empty if the
    #       model jumped straight to a draft — surface a minimal
    #       preamble so the draft card doesn't appear orphaned.
    chat_to_post = chat_text or (
        "Here's a draft you can review:" if draft_text else ":speech_balloon: (no reply)"
    )
    try:
        slack_client.post_message(channel_id, text=chat_to_post)
    except Exception as e:
        logger.warning("stay_agent: chat post failed: %s", e)

    # ── 7. Draft card (optional). The draft text is embedded on each
    #       button so the send/discard handlers don't need to re-read
    #       the journal to recover it — Slack action_ids + values are
    #       the durable carry-over.
    if draft_text:
        try:
            _post_draft_card(
                slack_client=slack_client,
                channel_id=channel_id,
                reservation_uuid=reservation_uuid,
                draft_text=draft_text,
                agent_entry_id=agent_entry.id,
            )
        except Exception as e:
            logger.warning("stay_agent: draft card post failed: %s", e)


# ─── Draft-card button handlers ─────────────────────────────────────────


def handle_send_draft(
    action_payload: dict,
    *,
    hospitable_client: Optional[HospitableClient] = None,
    slack_client: Optional[StayChannelSlackClient] = None,
    stay_store: Optional[StayStore] = None,
) -> None:
    """Send the guest-visible draft via Hospitable. Button handler.

    `action_payload` is the Slack `block_actions` payload. The clicked
    button carries `value` = the draft text and the block_id encodes
    `reservation_uuid`. Fast path — Slack expects a response in <3s.
    """
    slack_client = slack_client or StayChannelSlackClient()
    stay_store = stay_store or StayStore()
    hospitable_client = hospitable_client or HospitableClient()

    reservation_uuid, draft_text, container = _unpack_draft_action(action_payload)
    user = (action_payload.get("user") or {}).get("name", "someone")
    channel_id = container.get("channel_id", "")
    message_ts = container.get("message_ts", "")

    if not reservation_uuid or not draft_text or not channel_id:
        logger.warning("stay_agent: malformed send action: %s", action_payload)
        return

    # Call Hospitable. On failure, surface in-channel and journal it.
    try:
        hospitable_client.send_reservation_message(reservation_uuid, draft_text)
        sent = True
        err = ""
    except Exception as e:
        logger.exception("stay_agent: send to Hospitable failed: %s", e)
        sent = False
        err = str(e)

    now_iso = _now_iso()
    outcome = "sent" if sent else "failed"
    entry = StayEntry.new(
        source=SOURCE_SYSTEM,
        author=user,
        body=(
            f"Draft sent to guest by {user}" if sent
            else f"Draft send FAILED ({user}): {err}"
        ),
        metadata={
            "draft_state": outcome,
            "draft_text": draft_text,
            "slack_user": user,
        },
    )
    try:
        stay_store.append_entry(reservation_uuid, entry)
    except Exception as e:
        logger.warning("stay_agent: journal append on send failed: %s", e)

    # Replace the draft card with a status line.
    status_text = (
        f":white_check_mark: Sent to guest at {now_iso} by {user}" if sent
        else f":x: Send failed ({err}) — try again or copy/paste manually"
    )
    try:
        _update_card_as_status(slack_client, channel_id, message_ts, status_text)
    except Exception as e:
        logger.warning("stay_agent: card update after send failed: %s", e)


def handle_discard_draft(
    action_payload: dict,
    *,
    slack_client: Optional[StayChannelSlackClient] = None,
    stay_store: Optional[StayStore] = None,
) -> None:
    """Mark the draft as discarded. Button handler."""
    slack_client = slack_client or StayChannelSlackClient()
    stay_store = stay_store or StayStore()

    reservation_uuid, draft_text, container = _unpack_draft_action(action_payload)
    user = (action_payload.get("user") or {}).get("name", "someone")
    channel_id = container.get("channel_id", "")
    message_ts = container.get("message_ts", "")

    if not reservation_uuid or not channel_id:
        logger.warning("stay_agent: malformed discard action: %s", action_payload)
        return

    entry = StayEntry.new(
        source=SOURCE_SYSTEM,
        author=user,
        body=f"Draft discarded by {user}",
        metadata={
            "draft_state": "discarded",
            "draft_text": draft_text or "",
            "slack_user": user,
        },
    )
    try:
        stay_store.append_entry(reservation_uuid, entry)
    except Exception as e:
        logger.warning("stay_agent: journal append on discard failed: %s", e)

    try:
        _update_card_as_status(
            slack_client,
            channel_id,
            message_ts,
            f":wastebasket: Draft discarded by {user}",
        )
    except Exception as e:
        logger.warning("stay_agent: card update after discard failed: %s", e)


# ─── Context assembly ───────────────────────────────────────────────────


_SYSTEM_PROMPT_HEADER = """You are a hospitality expert collaborating with VJ \
and Maggie inside a private Slack channel dedicated to one short-term rental \
reservation. You have the full stay journal (every Hospitable message both \
directions, every chat turn with VJ/Maggie, every draft send/discard). Your \
mission: (1) make this guest's stay great, and (2) solve each situation ONCE — \
leave behind a KB update for repeated questions and a runbook for repeated \
physical work. The team should never handle the same thing twice.

People you can route work to (name only in prose — do NOT @mention):
- Allyson — on-the-ground point of contact (property access, vendor scheduling, coordination).
- Ashur — handyman (physical repairs, replacements, installs, debris, inspections).
- VJ and Maggie — approvers. They click "Send to guest" on the draft card and commit KB/runbook updates. Escalate to them only for emergencies or real policy calls.

Triage every incoming situation before responding. One message can span multiple categories — emit as many artifacts as apply in one turn:
1. EMERGENCY — safety (gas, fire, injury, security breach), lockout, no water/power/AC in Scottsdale heat, major property damage, legal exposure. Flag with <<EMERGENCY>>; do NOT also emit a <<DRAFT>>.
2. Operational / physical work — something must physically happen at the property. Emit a <<RUNBOOK>> and usually a <<DRAFT>> acknowledging the guest.
3. Guest question the KB already answers — emit a <<DRAFT>> from the KB. Do NOT add a duplicate <<KB_UPDATE>>.
4. Guest question the KB does NOT yet cover — emit a <<DRAFT>> plus a <<KB_UPDATE>> adding a new FAQ.
5. Novel policy/judgment call (vendor type, refund, special request not in precedents) — emit a <<DRAFT>> plus a <<KB_UPDATE>> adding a precedent.
6. Chat with VJ/Maggie (they are asking YOU, not the guest) — just answer in chat. No tags.

Take the obvious hospitality move without asking VJ/Maggie for permission on standard-of-care defaults (acknowledge an issue, offer a replacement for a broken amenity, apologize and remediate for sensory issues, confirm a late checkout when the calendar allows). Ask only when there is a real policy call to make.

Output structure. All tags are plain text, each on its own line. Text OUTSIDE tags is your chat with VJ/Maggie (reasoning, context, questions). Only <<DRAFT>> content is ever guest-visible — VJ or Maggie clicks "Send to guest" on the draft card. NEVER send a message to the guest yourself.

- <<DRAFT>> ... <<END_DRAFT>> — the guest-visible reply.
  * Plain text only. No markdown. Hospitable forwards to Airbnb/Vrbo which do not render markdown.
  * Short and warm. Mirror the guest's register. Echo the guest's exact names for people, services, products, places.
  * For a PRE-BOOKING inquiry (reservation status: inquiry), never share Wi-Fi, door/access codes, or the exact street address.
  * Honor decisions already logged for this reservation. Do not propose redoing something the team already decided.

- <<EMERGENCY>> ... <<END_EMERGENCY>> — one or two sentences: what is happening, and what VJ or Maggie must do now. Use only for real safety/urgency triggers — this is the escalation signal.

- <<KB_UPDATE file=src/knowledge_base/<file>.yaml>> ... <<END_KB_UPDATE>> — a YAML snippet that pastes cleanly into the named property's KB file. Use file=src/knowledge_base/palm_club.yaml for The Palm Club, file=src/knowledge_base/villa_bougainvillea.yaml for Villa Bougainvillea — derive from the Reservation block below. One update per block; multiple blocks per turn allowed. Three valid shapes:

    New FAQ under dynamic.faqs:
      - topic: <short_snake_case_slug>
        question: <guest's question in plain English>
        answer: <answer suitable to paste into a guest reply>
        source_reservations: [<this reservation_uuid>]
        last_seen: <YYYY-MM-DD>

    New precedent under dynamic.precedents:
      - situation: <what came up>
        what_we_did: <how we handled it; the policy going forward>
        source_reservations: [<this reservation_uuid>]

    Static-fact edit (rare): a static.<path>: <new value> snippet with a one-line note on what it changes and why.

  Before emitting a <<KB_UPDATE>>, check the KB block below. If an FAQ or precedent already covers the situation, do NOT add a duplicate — just draft from it.

- <<RUNBOOK for=Ashur poc=Allyson>> ... <<END_RUNBOOK>> — plain-text work order. Include: what needs to happen, where in the property, deadline (before next check-in / within 24h / etc.), access instructions (coordinate with Allyson), and acceptance criteria ("coffee pot replaced and brews a full pot cleanly").
"""


def _build_system_prompt(
    *,
    reservation_context: str,
    kb_text: str,
    thread_logs_text: str,
) -> str:
    """Assemble the system prompt. Reservation summary + KB + thread
    logs are static per-turn; the live conversation goes through the
    `messages` array."""
    parts = [_SYSTEM_PROMPT_HEADER]
    if reservation_context:
        parts.append("─── Reservation ───\n" + reservation_context)
    if kb_text:
        parts.append("─── Property knowledge base (authoritative) ───\n" + kb_text[:6000])
    if thread_logs_text:
        parts.append("─── Prior internal notes/issues/resolutions ───\n" + thread_logs_text[:2000])
    return "\n\n".join(parts)


def _build_turns(
    *,
    journal,
    trigger_text: str,
    trigger_user_id: str,
) -> list[dict]:
    """Journal entries → multi-turn `messages` for the Anthropic SDK.

    Mapping:
    - `hospitable-guest`, `slack-user` → role: user
    - `hospitable-host`, `slack-agent` → role: assistant
    - `system` → skipped (reflected in the system prompt, not here)

    The triggering message is also journaled (see
    `_handle_channel_message` step 1). It'll already be the last user
    turn in this list — no need to append it again.

    Anthropic requires the final turn to be `role: user`. If the
    journal's last user-mapped entry isn't ours, append the trigger
    text explicitly as a safety net.
    """
    entries = (journal.entries if journal else [])[-_RECENT_ENTRIES_WINDOW:]
    turns: list[dict] = []

    def _role_for(source: str) -> Optional[str]:
        if source in (SOURCE_HOSPITABLE_GUEST, SOURCE_SLACK_USER):
            return "user"
        if source in (SOURCE_HOSPITABLE_HOST, SOURCE_SLACK_AGENT):
            return "assistant"
        return None  # SYSTEM entries live in the system prompt only

    for e in entries:
        role = _role_for(e.source)
        if not role:
            continue
        body = _render_entry_body(e)
        if not body:
            continue
        # Collapse adjacent same-role turns into one message, as the
        # Anthropic API requires strict alternation.
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] = turns[-1]["content"] + "\n\n" + body
        else:
            turns.append({"role": role, "content": body})

    if not turns or turns[-1]["role"] != "user":
        # Safety net — shouldn't normally hit this since we just
        # journaled the trigger as slack-user.
        turns.append({"role": "user", "content": trigger_text})
    return turns


def _render_entry_body(entry: StayEntry) -> str:
    """Compact per-entry text for the model."""
    if entry.source == SOURCE_HOSPITABLE_GUEST:
        return f"[Guest, via Hospitable] {entry.body}"
    if entry.source == SOURCE_HOSPITABLE_HOST:
        return f"[Us, via Hospitable] {entry.body}"
    if entry.source == SOURCE_SLACK_USER:
        return f"[{entry.author} in Slack] {entry.body}"
    if entry.source == SOURCE_SLACK_AGENT:
        # Agent already-sent output — include as-is so the model sees
        # its prior reasoning + draft tags.
        return entry.body
    return ""


def _load_reservation_context(stay_row: dict) -> str:
    """Compact reservation/property/guest block from the stay row.

    Durable (DynamoDB) — no live Hospitable call per turn. The
    journal's `reservation_snapshot` has full detail if the agent
    wants to dig deeper via the multi-turn history.
    """
    bits = []
    if stay_row.get("property_name"):
        bits.append(f"Property: {stay_row['property_name']}")
    if stay_row.get("guest_name"):
        bits.append(f"Guest: {stay_row['guest_name']}")
    ci = stay_row.get("check_in", "")
    co = stay_row.get("check_out", "")
    if ci and co:
        bits.append(f"Dates: {ci} → {co}")
    return "\n".join(bits)


def _load_kb_text(property_id: str) -> str:
    if not property_id:
        return ""
    try:
        return format_kb(load_kb(property_id))
    except Exception as e:
        logger.warning("stay_agent: KB load failed for %s: %s", property_id, e)
        return ""


def _load_thread_logs_text(reservation_uuid: str) -> str:
    try:
        return format_logs(ThreadLogs().get_logs(reservation_uuid))
    except Exception as e:
        logger.warning(
            "stay_agent: thread logs load failed for %s: %s",
            reservation_uuid,
            e,
        )
        return ""


# ─── Claude call ────────────────────────────────────────────────────────


def _call_claude(system_prompt: str, messages: list[dict]) -> str:
    """Single-turn Claude call. Returns the model's full text output."""
    client = _get_anthropic_client()
    response = client.messages.create(
        model=DRAFT_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text.strip() if response.content else ""


# ─── Draft handling ─────────────────────────────────────────────────────


def _extract_draft(response_text: str) -> tuple[str, str]:
    """Split the model output into (chat_text, draft_text).

    Looks for `<<DRAFT>>` / `<<END_DRAFT>>` tags each on their own line
    (or inline — we accept both). If either tag is missing the whole
    output is treated as chat and no draft is surfaced.
    """
    if not response_text:
        return "", ""
    open_idx = response_text.find(_DRAFT_OPEN)
    close_idx = response_text.find(_DRAFT_CLOSE)
    if open_idx == -1 or close_idx == -1 or close_idx <= open_idx:
        return response_text.strip(), ""
    chat = response_text[:open_idx].strip()
    draft = response_text[open_idx + len(_DRAFT_OPEN) : close_idx].strip()
    # Also drop anything after the closing tag — occasionally the
    # model adds a trailing comment.
    return chat, draft


def _post_draft_card(
    *,
    slack_client: StayChannelSlackClient,
    channel_id: str,
    reservation_uuid: str,
    draft_text: str,
    agent_entry_id: str,
) -> dict:
    """Block Kit draft card with Send / Discard buttons.

    The draft text is embedded in each button's `value` field so the
    button handlers don't need to re-read the journal to recover the
    text that was proposed. Slack caps `value` at 2000 chars; we
    truncate defensively and tell the user if that happened.
    """
    truncated = len(draft_text) > 1800
    carried_draft = draft_text[:1800] if truncated else draft_text

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Draft reply (not sent):*\n> {draft_text}"
                + ("\n\n_…draft was long; Send button carries truncated text._" if truncated else ""),
            },
        },
        {
            "type": "actions",
            "block_id": f"stay_draft:{reservation_uuid}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "stay_send_draft",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Send to guest"},
                    "value": carried_draft,
                },
                {
                    "type": "button",
                    "action_id": "stay_discard_draft",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Discard"},
                    "value": agent_entry_id,
                },
            ],
        },
    ]
    return slack_client.post_message(
        channel_id,
        text="Draft reply ready — Send to guest / Discard",
        blocks=blocks,
    )


def _unpack_draft_action(action_payload: dict) -> tuple[str, str, dict]:
    """Return (reservation_uuid, draft_text, container) from a
    block_actions payload."""
    actions = action_payload.get("actions") or []
    if not actions:
        return "", "", {}
    action = actions[0]
    block_id = action.get("block_id") or ""
    reservation_uuid = ""
    if block_id.startswith("stay_draft:"):
        reservation_uuid = block_id.split(":", 1)[1]
    draft_text = action.get("value") or ""
    container = action_payload.get("container") or {}
    return reservation_uuid, draft_text, container


def _update_card_as_status(
    slack_client: StayChannelSlackClient,
    channel_id: str,
    message_ts: str,
    status_text: str,
) -> dict:
    """Overwrite the draft card's blocks with a one-line status."""
    import requests

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": status_text}},
    ]
    r = requests.post(
        f"{StayChannelSlackClient._SLACK_BASE}/chat.update",
        headers={
            "Authorization": f"Bearer {get_slack_bot_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "channel": channel_id,
            "ts": message_ts,
            "text": status_text,
            "blocks": blocks,
        },
    )
    return r.json()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
