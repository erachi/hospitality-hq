"""Tests for the Block Kit builders. Purely structural — verify we produce
valid shapes, not pixel-perfect text.
"""

from task_models import (
    Task,
    STATUS_DONE,
    STATUS_BLOCKED,
    PRIORITY_HIGH,
    PROPERTY_BUSINESS,
)
from task_slack_ui import (
    build_create_modal,
    build_task_card_blocks,
    build_task_list_blocks,
    CREATE_MODAL_CALLBACK_ID,
)


PROPERTIES = [
    {"id": "prop-palm", "name": "The Palm Club", "slug": "palm"},
    {"id": "prop-villa", "name": "Villa Bougainvillea", "slug": "villa"},
]
USERS = [
    {"id": "vj", "slack_user_id": "UVJ", "display_name": "VJ"},
    {"id": "maggie", "slack_user_id": "UMAGGIE", "display_name": "Maggie"},
]


def _task(**kw):
    defaults = dict(
        title="Fix disposal",
        description="",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="maggie",
    )
    defaults.update(kw)
    return Task.new(**defaults)


def test_create_modal_shape():
    view = build_create_modal(properties=PROPERTIES, users=USERS)

    assert view["type"] == "modal"
    assert view["callback_id"] == CREATE_MODAL_CALLBACK_ID
    assert view["submit"]["text"] == "Create"

    block_ids = [b.get("block_id") for b in view["blocks"] if b.get("block_id")]
    assert "title_block" in block_ids
    assert "property_block" in block_ids
    assert "assignee_block" in block_ids
    assert "priority_block" in block_ids


def test_create_modal_includes_business_wide_option():
    view = build_create_modal(properties=PROPERTIES, users=USERS)
    property_block = next(b for b in view["blocks"] if b.get("block_id") == "property_block")
    option_values = [o["value"] for o in property_block["element"]["options"]]
    assert PROPERTY_BUSINESS in option_values
    assert "prop-palm" in option_values


def test_task_card_has_action_buttons_when_open():
    t = _task(title="Open task")
    blocks = build_task_card_blocks(t, properties=PROPERTIES, users=USERS)

    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 1
    action_ids = [e["action_id"] for e in actions[0]["elements"]]
    assert "task_mark_done" in action_ids
    assert "task_swap_assignee" in action_ids


def test_task_card_reopen_only_when_done():
    t = _task()
    t.status = STATUS_DONE
    blocks = build_task_card_blocks(t, properties=PROPERTIES, users=USERS)

    actions = next(b for b in blocks if b.get("type") == "actions")
    action_ids = [e["action_id"] for e in actions["elements"]]
    assert action_ids == ["task_reopen"]


def test_task_card_renders_business_wide_label():
    t = _task(property_id=PROPERTY_BUSINESS, title="Scottsdale compliance")
    blocks = build_task_card_blocks(t, properties=PROPERTIES, users=USERS)

    # Convert all blocks to a flat text for assertion
    flat = str(blocks)
    assert "Business-wide" in flat


def test_list_blocks_empty():
    blocks = build_task_list_blocks(
        [],
        title="Your open tasks",
        empty_text="_Nothing on your plate._",
        properties=PROPERTIES,
        users=USERS,
    )
    assert any("Nothing" in str(b) for b in blocks)


def test_list_blocks_sorts_overdue_first():
    future = _task(title="future", due_date="2099-01-01")
    overdue = _task(title="overdue", due_date="2020-01-01")

    blocks = build_task_list_blocks(
        [future, overdue],
        title="t",
        empty_text="_empty_",
        properties=PROPERTIES,
        users=USERS,
    )
    # Convert to flat strings and check order
    task_rows = [b for b in blocks if b.get("type") == "section"]
    assert "overdue" in str(task_rows[0])
    assert "future" in str(task_rows[1])


def test_list_blocks_mark_done_accessory_only_on_open():
    done = _task(title="done one")
    done.status = STATUS_DONE
    open_t = _task(title="open one")

    blocks = build_task_list_blocks(
        [open_t, done],
        title="mixed",
        empty_text="_empty_",
        properties=PROPERTIES,
        users=USERS,
        # include_closed would be handled upstream; this tests rendering both
    )
    # Both render; only open one gets the accessory button
    sections = [b for b in blocks if b.get("type") == "section"]
    accessories = [s for s in sections if "accessory" in s]
    assert len(accessories) == 1
    assert "open one" in str(accessories[0])
