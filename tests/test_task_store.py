"""Tests for the S3-backed TaskStore."""

import json

from task_models import Task, STATUS_DONE, PRIORITY_HIGH, PROPERTY_BUSINESS
from task_store import TaskStore


def _make_task(**overrides) -> Task:
    defaults = dict(
        title="do the thing",
        description="details",
        property_id="prop-palm",
        assignee_id="vj",
        created_by_id="maggie",
    )
    defaults.update(overrides)
    return Task.new(**defaults)


def test_put_and_get(tasks_bucket):
    store = TaskStore()
    task = _make_task(title="Fix AC at Palm")

    store.put(task)
    fetched = store.get(task.id)

    assert fetched is not None
    assert fetched.title == "Fix AC at Palm"
    assert fetched.assignee_id == "vj"


def test_get_missing_returns_none(tasks_bucket):
    store = TaskStore()
    assert store.get("does-not-exist") is None


def test_list_all_filters_closed_by_default(tasks_bucket):
    store = TaskStore()
    open_task = _make_task(title="open one")
    done_task = _make_task(title="done one")
    done_task.status = STATUS_DONE

    store.put(open_task)
    store.put(done_task)

    open_only = store.list_all()
    assert {t.id for t in open_only} == {open_task.id}

    everything = store.list_all(include_closed=True)
    assert {t.id for t in everything} == {open_task.id, done_task.id}


def test_list_by_assignee(tasks_bucket):
    store = TaskStore()
    t1 = _make_task(assignee_id="vj", title="vj task")
    t2 = _make_task(assignee_id="maggie", title="maggie task")
    store.put(t1)
    store.put(t2)

    vj_tasks = store.list_by_assignee("vj")
    assert [t.title for t in vj_tasks] == ["vj task"]


def test_list_by_property(tasks_bucket):
    store = TaskStore()
    palm = _make_task(property_id="prop-palm", title="palm")
    villa = _make_task(property_id="prop-villa", title="villa")
    biz = _make_task(property_id=PROPERTY_BUSINESS, title="compliance")
    for t in (palm, villa, biz):
        store.put(t)

    assert [t.title for t in store.list_by_property("prop-palm")] == ["palm"]
    assert [t.title for t in store.list_by_property(PROPERTY_BUSINESS)] == ["compliance"]


def test_list_overdue(tasks_bucket):
    store = TaskStore()
    past = _make_task(title="overdue", due_date="2025-01-01")
    future = _make_task(title="not yet", due_date="2099-12-31")
    no_due = _make_task(title="no deadline")
    done_past = _make_task(title="done and past", due_date="2025-01-01")
    done_past.status = STATUS_DONE
    for t in (past, future, no_due, done_past):
        store.put(t)

    overdue = store.list_overdue(today="2026-04-20")
    assert [t.title for t in overdue] == ["overdue"]


def test_slack_index_lookup(tasks_bucket):
    store = TaskStore()
    task = _make_task(title="with thread")
    store.put(task)
    store.put_slack_index(thread_ts="1700000000.000001", task_id=task.id)

    fetched = store.get_task_by_thread("1700000000.000001")
    assert fetched is not None
    assert fetched.id == task.id

    missing = store.get_task_by_thread("does-not-exist")
    assert missing is None


def test_get_user_by_slack_id(tasks_bucket):
    store = TaskStore()
    assert store.get_user_by_slack_id("UVJ")["id"] == "vj"
    assert store.get_user_by_slack_id("UMAGGIE")["id"] == "maggie"
    assert store.get_user_by_slack_id("nope") is None


def test_get_property(tasks_bucket):
    store = TaskStore()
    assert store.get_property("prop-palm")["slug"] == "palm"
    assert store.get_property("missing") is None
