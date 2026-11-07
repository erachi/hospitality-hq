"""Tests for the ambient Claude agent and draft-button handlers.

These tests are structured so the Claude SDK and Slack calls are
mocked; the real logic exercised is the journal-first invariant, the
draft tag extraction, the multi-turn `messages` construction, and the
button handlers that fire the Hospitable send path.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from moto import mock_aws

import stay_agent
from stay_agent import (
    _build_turns,
    _extract_draft,
    _render_entry_body,
    handle_channel_message,
    handle_discard_draft,
    handle_send_draft,
)
from stay_channels import StayChannels
from stay_models import (
    SOURCE_HOSPITABLE_GUEST,
    SOURCE_HOSPITABLE_HOST,
    SOURCE_SLACK_AGENT,
    SOURCE_SLACK_USER,
    SOURCE_SYSTEM,
    StayEntry,
    StayJournal,
)
from stay_store import StayStore


# ─── _extract_draft ─────────────────────────────────────────────────────


def test_extract_draft_separates_chat_and_draft():
    text = (
        "Sure, here's my attempt — I kept it light.\n\n"
        "<<DRAFT>>\n"
        "Hi Jane, totally fine if you drop bags at 11, see you at 3!\n"
        "<<END_DRAFT>>"
    )
    chat, draft = _extract_draft(text)
    assert chat.startswith("Sure")
    assert draft.startswith("Hi Jane")
    assert "<<DRAFT>>" not in draft


def test_extract_draft_returns_empty_when_tags_absent():
    chat, draft = _extract_draft("Just chatting, no draft here.")
    assert chat == "Just chatting, no draft here."
    assert draft == ""


def test_extract_draft_ignores_trailing_text_after_close():
    text = "chat\n<<DRAFT>>\nhello guest\n<<END_DRAFT>>\n--trailing comment--"
    chat, draft = _extract_draft(text)
    assert chat == "chat"
    assert draft == "hello guest"


def test_extract_draft_handles_missing_close_tag():
    """Half-emitted draft should NOT leak the <<DRAFT>> tag to the user."""
    chat, draft = _extract_draft("chat\n<<DRAFT>>\nhalf-written...")
    assert draft == ""
    # The whole thing stays in chat including the open tag so the user
    # notices the model misbehaved.
    assert "half-written" in chat


# ─── _build_turns ───────────────────────────────────────────────────────


def _journal_with_entries(*entries) -> StayJournal:
    j = StayJournal.new(
        reservation_uuid="res-x",
        property_id="prop-palm",
        property_name="The Palm Club",
        guest_name="Jane",
        check_in="2026-11-20",
        check_out="2026-11-25",
    )
    for e in entries:
        j.append(e)
    return j


def test_build_turns_maps_sources_to_correct_roles():
    j = _journal_with_entries(
        StayEntry.new(source=SOURCE_HOSPITABLE_GUEST, author="guest", body="Hi"),
        StayEntry.new(source=SOURCE_HOSPITABLE_HOST, author="host", body="Hey"),
        StayEntry.new(source=SOURCE_SLACK_USER, author="UVJ", body="should we offer?"),
        StayEntry.new(source=SOURCE_SLACK_AGENT, author="agent", body="Sure, here:"),
    )
    turns = _build_turns(journal=j, trigger_text="should we offer?", trigger_user_id="UVJ")
    roles = [t["role"] for t in turns]
    # Alternating by API requirement; agent must land on user.
    assert roles[0] == "user"
    for i in range(1, len(roles)):
        assert roles[i] != roles[i - 1], f"turns must alternate, got {roles}"
    assert roles[-1] == "user"


def test_build_turns_skips_system_entries():
    j = _journal_with_entries(
        StayEntry.new(source=SOURCE_SYSTEM, author="system", body="provisioned"),
        StayEntry.new(source=SOURCE_SLACK_USER, author="UVJ", body="hi"),
    )
    turns = _build_turns(journal=j, trigger_text="hi", trigger_user_id="UVJ")
    for t in turns:
        assert "provisioned" not in t["content"]


def test_build_turns_collapses_consecutive_same_role():
    j = _journal_with_entries(
        StayEntry.new(source=SOURCE_HOSPITABLE_GUEST, author="guest", body="first"),
        StayEntry.new(source=SOURCE_SLACK_USER, author="UVJ", body="second"),
    )
    turns = _build_turns(journal=j, trigger_text="second", trigger_user_id="UVJ")
    # Both entries map to "user" and must collapse into one turn.
    user_turns = [t for t in turns if t["role"] == "user"]
    assert len(user_turns) == 1
    assert "first" in user_turns[0]["content"]
    assert "second" in user_turns[0]["content"]


def test_build_turns_appends_trigger_when_journal_empty():
    j = _journal_with_entries()
    turns = _build_turns(journal=j, trigger_text="hello", trigger_user_id="UVJ")
    assert turns == [{"role": "user", "content": "hello"}]


def test_render_entry_body_labels_sources():
    e_guest = StayEntry.new(source=SOURCE_HOSPITABLE_GUEST, author="guest", body="hi")
    e_user = StayEntry.new(source=SOURCE_SLACK_USER, author="UVJ", body="thoughts?")
    assert _render_entry_body(e_guest).startswith("[Guest, via Hospitable]")
    assert _render_entry_body(e_user).startswith("[UVJ in Slack]")


# ─── handle_channel_message — journal-first + Claude mock ──────────────


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(stay_agent, "STAY_CHANNELS_ENABLED", True, raising=False)


def _seed_live_stay() -> dict:
    """Insert a StayChannels row + seed a journal with one entry."""
    sc = StayChannels()
    sc.put(
        reservation_uuid="res-x",
        channel_id="C_STAY",
        channel_name="stay-palm-smith-20261120",
        property_id="prop-palm",
        property_name="The Palm Club",
        guest_name="Jane Smith",
        check_in="2026-11-20",
        check_out="2026-11-25",
        journal_s3_key="stays/res-x.json",
    )
    store = StayStore()
    j = _journal_with_entries(
        StayEntry.new(source=SOURCE_HOSPITABLE_GUEST, author="guest", body="hi there")
    )
    store.put(j)
    return sc.get_by_reservation("res-x")


def _claude_response(text: str):
    """Shape an Anthropic SDK response object stub."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


@mock_aws
def test_handle_channel_message_journals_user_and_agent_turns(
    flag_on, stay_channels_table, stay_journal_bucket
):
    row = _seed_live_stay()
    slack = MagicMock()
    slack.post_message.return_value = {"ok": True, "ts": "1700000000.1"}

    with patch("stay_agent._call_claude", return_value="Looks solid to me."):
        handle_channel_message(
            {"text": "What do you think?", "user": "UVJ", "ts": "1700000000.5"},
            row,
            slack_client=slack,
        )

    journal = StayStore().get("res-x")
    sources = [e.source for e in journal.entries]
    # Original guest message + our new user turn + agent turn.
    assert sources == [
        SOURCE_HOSPITABLE_GUEST,
        SOURCE_SLACK_USER,
        SOURCE_SLACK_AGENT,
    ]
    # User turn was appended FIRST (lower index than the agent turn).
    user_idx = sources.index(SOURCE_SLACK_USER)
    agent_idx = sources.index(SOURCE_SLACK_AGENT)
    assert user_idx < agent_idx
    assert journal.entries[user_idx].body == "What do you think?"
    assert journal.entries[agent_idx].body == "Looks solid to me."

    # Chat was posted; no draft card (no <<DRAFT>> tag in the model output).
    assert slack.post_message.call_count == 1


@mock_aws
def test_handle_channel_message_posts_draft_card_when_draft_tag_present(
    flag_on, stay_channels_table, stay_journal_bucket
):
    row = _seed_live_stay()
    slack = MagicMock()
    slack.post_message.return_value = {"ok": True, "ts": "1700000000.1"}
    model_output = (
        "I'd go with a light yes.\n"
        "<<DRAFT>>\nHi Jane, happy to confirm — see you Friday!\n<<END_DRAFT>>"
    )
    with patch("stay_agent._call_claude", return_value=model_output):
        handle_channel_message(
            {"text": "Can we confirm?", "user": "UVJ", "ts": "1.1"},
            row,
            slack_client=slack,
        )

    # Two posts: the chat preamble and the draft card.
    assert slack.post_message.call_count == 2
    # Second call carries the Block Kit draft card with Send/Discard buttons.
    second_kwargs = slack.post_message.call_args_list[1].kwargs
    blocks = second_kwargs.get("blocks") or []
    assert any(b.get("type") == "actions" for b in blocks)
    action_block = next(b for b in blocks if b.get("type") == "actions")
    action_ids = [e.get("action_id") for e in action_block["elements"]]
    assert "stay_send_draft" in action_ids
    assert "stay_discard_draft" in action_ids

    # Journal recorded the draft metadata on the agent turn.
    journal = StayStore().get("res-x")
    agent_entries = [e for e in journal.entries if e.source == SOURCE_SLACK_AGENT]
    assert agent_entries
    assert agent_entries[-1].metadata["has_draft"] is True
    assert "Hi Jane" in agent_entries[-1].metadata["draft_text"]


@mock_aws
def test_handle_channel_message_apologizes_when_claude_errors(
    flag_on, stay_channels_table, stay_journal_bucket
):
    row = _seed_live_stay()
    slack = MagicMock()
    slack.post_message.return_value = {"ok": True, "ts": "1.1"}
    with patch("stay_agent._call_claude", side_effect=RuntimeError("boom")):
        handle_channel_message(
            {"text": "What do you think?", "user": "UVJ", "ts": "1.1"},
            row,
            slack_client=slack,
        )

    # One Slack post — the apology line.
    assert slack.post_message.call_count == 1
    posted_text = slack.post_message.call_args.kwargs.get("text", "")
    assert "error" in posted_text.lower()

    # The user turn still got journaled even though Claude blew up.
    journal = StayStore().get("res-x")
    sources = [e.source for e in journal.entries]
    assert SOURCE_SLACK_USER in sources


@mock_aws
def test_handle_channel_message_swallows_empty_text(
    flag_on, stay_channels_table, stay_journal_bucket
):
    row = _seed_live_stay()
    slack = MagicMock()
    with patch("stay_agent._call_claude") as mock_claude:
        handle_channel_message(
            {"text": "   ", "user": "UVJ", "ts": "1.1"},
            row,
            slack_client=slack,
        )
    mock_claude.assert_not_called()
    slack.post_message.assert_not_called()


# ─── Send / Discard button handlers ─────────────────────────────────────


def _draft_action_payload(
    *,
    action_id: str,
    draft_text: str = "Hi Jane, confirmed!",
    reservation_uuid: str = "res-x",
    channel_id: str = "C_STAY",
    message_ts: str = "1700000000.900",
    user_name: str = "vj",
) -> dict:
    """Shape of a Slack block_actions payload."""
    return {
        "type": "block_actions",
        "user": {"name": user_name, "id": "UVJ"},
        "container": {"channel_id": channel_id, "message_ts": message_ts},
        "actions": [
            {
                "action_id": action_id,
                "block_id": f"stay_draft:{reservation_uuid}",
                "value": draft_text,
            }
        ],
    }


@mock_aws
def test_send_draft_calls_hospitable_and_journals_sent(
    stay_channels_table, stay_journal_bucket
):
    _seed_live_stay()
    hospitable = MagicMock()
    hospitable.send_reservation_message.return_value = {"ok": True}
    slack = MagicMock()

    with patch("stay_agent._update_card_as_status") as mock_update:
        handle_send_draft(
            _draft_action_payload(action_id="stay_send_draft"),
            hospitable_client=hospitable,
            slack_client=slack,
        )

    hospitable.send_reservation_message.assert_called_once_with(
        "res-x", "Hi Jane, confirmed!"
    )
    mock_update.assert_called_once()
    # Card was updated with a "Sent" status.
    status_text = mock_update.call_args.args[-1]
    assert "Sent" in status_text or "sent" in status_text

    # Journal records a system entry with draft_state=sent.
    journal = StayStore().get("res-x")
    last = journal.entries[-1]
    assert last.source == SOURCE_SYSTEM
    assert last.metadata["draft_state"] == "sent"


@mock_aws
def test_send_draft_records_failure_when_hospitable_errors(
    stay_channels_table, stay_journal_bucket
):
    _seed_live_stay()
    hospitable = MagicMock()
    hospitable.send_reservation_message.side_effect = RuntimeError("500 bad")
    slack = MagicMock()

    with patch("stay_agent._update_card_as_status") as mock_update:
        handle_send_draft(
            _draft_action_payload(action_id="stay_send_draft"),
            hospitable_client=hospitable,
            slack_client=slack,
        )

    mock_update.assert_called_once()
    status_text = mock_update.call_args.args[-1]
    assert "fail" in status_text.lower() or "✗" in status_text or ":x:" in status_text

    journal = StayStore().get("res-x")
    last = journal.entries[-1]
    assert last.metadata["draft_state"] == "failed"


@mock_aws
def test_discard_draft_updates_card_and_journals(
    stay_channels_table, stay_journal_bucket
):
    _seed_live_stay()
    slack = MagicMock()

    with patch("stay_agent._update_card_as_status") as mock_update:
        handle_discard_draft(
            _draft_action_payload(action_id="stay_discard_draft"),
            slack_client=slack,
        )

    mock_update.assert_called_once()
    status_text = mock_update.call_args.args[-1]
    assert "discard" in status_text.lower()

    journal = StayStore().get("res-x")
    last = journal.entries[-1]
    assert last.metadata["draft_state"] == "discarded"
