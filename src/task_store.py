"""S3-backed storage for tasks, plus static config (properties, users).

Layout:
    s3://<bucket>/
    ├── config/
    │   ├── properties.json   # list of {id, name, slug, ...}
    │   └── users.json        # list of {id, slack_user_id, display_name}
    ├── tasks/
    │   └── <task_id>.json    # one file per task, full state incl. comments
    └── indexes/
        └── slack/<thread_ts>.json   # pointer: {"task_id": "..."}

Rationale: for ~200 tasks/year and two users, a flat JSON store keeps the
read path honest (LIST + parallel GET, or a single GET by id) and makes
debugging as simple as `aws s3 cp s3://... - | jq`. S3 versioning is the
free audit trail.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from config import TASKS_BUCKET
from task_models import Task, OPEN_STATUSES


logger = logging.getLogger(__name__)

# Key prefixes
_TASKS_PREFIX = "tasks/"
_CONFIG_PREFIX = "config/"
_INDEX_SLACK_PREFIX = "indexes/slack/"

# Parallelism for the LIST+GET fan-out. 20 is well within Lambda's network
# capacity and keeps full-list queries comfortably under 500ms at our scale.
_LIST_PARALLELISM = 20


@lru_cache(maxsize=1)
def _s3():
    """Cached S3 client — reused across warm Lambda invocations."""
    return boto3.client("s3")


class TaskStore:
    """Read/write tasks to S3. Concurrency is handled by ETag conditional
    writes where it matters; otherwise we accept last-writer-wins for two
    users who are unlikely to race.
    """

    def __init__(self, bucket: Optional[str] = None):
        self.bucket = bucket or TASKS_BUCKET

    # ─── Task CRUD ───────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[Task]:
        """Fetch one task by id. Returns None if missing."""
        if not task_id:
            return None
        try:
            obj = _s3().get_object(Bucket=self.bucket, Key=_task_key(task_id))
            data = json.loads(obj["Body"].read())
            return Task.from_dict(data)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, task: Task) -> None:
        """Write a task. Touches updated_at."""
        task.touch()
        body = json.dumps(task.to_dict(), indent=2).encode("utf-8")
        _s3().put_object(
            Bucket=self.bucket,
            Key=_task_key(task.id),
            Body=body,
            ContentType="application/json",
        )

    def delete(self, task_id: str) -> None:
        """Delete a task. S3 versioning keeps the history if enabled."""
        _s3().delete_object(Bucket=self.bucket, Key=_task_key(task_id))

    def list_all(self, include_closed: bool = False) -> list[Task]:
        """List every task. At our scale (hundreds), this is fine.

        If include_closed is False, filters to open statuses client-side —
        simpler than maintaining separate prefixes.
        """
        keys = self._list_keys(_TASKS_PREFIX)
        if not keys:
            return []

        tasks = self._get_many(keys)
        if not include_closed:
            tasks = [t for t in tasks if t.status in OPEN_STATUSES]
        return tasks

    def list_by_assignee(self, assignee_id: str, include_closed: bool = False) -> list[Task]:
        tasks = self.list_all(include_closed=include_closed)
        return [t for t in tasks if t.assignee_id == assignee_id]

    def list_by_property(self, property_id: str, include_closed: bool = False) -> list[Task]:
        tasks = self.list_all(include_closed=include_closed)
        return [t for t in tasks if t.property_id == property_id]

    def list_overdue(self, today: Optional[str] = None) -> list[Task]:
        """All open tasks with a due_date earlier than today."""
        return [t for t in self.list_all(include_closed=False) if t.is_overdue(today)]

    # ─── Slack thread pointer index ──────────────────────────────────────

    def put_slack_index(self, thread_ts: str, task_id: str) -> None:
        """Write a pointer so thread replies can resolve back to a task id.

        Pointer objects are write-once at task creation — no update hazard.
        """
        if not thread_ts or not task_id:
            return
        _s3().put_object(
            Bucket=self.bucket,
            Key=_slack_index_key(thread_ts),
            Body=json.dumps({"task_id": task_id}).encode("utf-8"),
            ContentType="application/json",
        )

    def get_task_by_thread(self, thread_ts: str) -> Optional[Task]:
        """Resolve a Slack thread_ts to the full Task (two GETs)."""
        if not thread_ts:
            return None
        try:
            obj = _s3().get_object(Bucket=self.bucket, Key=_slack_index_key(thread_ts))
            pointer = json.loads(obj["Body"].read())
            return self.get(pointer.get("task_id", ""))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    # ─── Config: properties and users ────────────────────────────────────

    @lru_cache(maxsize=1)
    def load_properties(self) -> list[dict]:
        """Load the static properties list. Cached per Lambda cold start."""
        return self._load_config("properties.json")

    @lru_cache(maxsize=1)
    def load_users(self) -> list[dict]:
        """Load the static users list. Cached per Lambda cold start."""
        return self._load_config("users.json")

    def get_property(self, property_id: str) -> Optional[dict]:
        for p in self.load_properties():
            if p.get("id") == property_id:
                return p
        return None

    def get_user(self, user_id: str) -> Optional[dict]:
        for u in self.load_users():
            if u.get("id") == user_id:
                return u
        return None

    def get_user_by_slack_id(self, slack_user_id: str) -> Optional[dict]:
        for u in self.load_users():
            if u.get("slack_user_id") == slack_user_id:
                return u
        return None

    def _load_config(self, name: str) -> list[dict]:
        try:
            obj = _s3().get_object(Bucket=self.bucket, Key=_CONFIG_PREFIX + name)
            return json.loads(obj["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                logger.warning(f"Config file {name} not found in bucket {self.bucket}")
                return []
            raise

    # ─── Internals ───────────────────────────────────────────────────────

    def _list_keys(self, prefix: str) -> list[str]:
        """List every key under a prefix, paginated."""
        keys = []
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                # Skip pseudo-directories if any.
                if key.endswith("/"):
                    continue
                keys.append(key)
        return keys

    def _get_many(self, keys: list[str]) -> list[Task]:
        """Parallel fan-out GET for a list of task keys."""

        def _fetch(key: str) -> Optional[Task]:
            try:
                obj = _s3().get_object(Bucket=self.bucket, Key=key)
                return Task.from_dict(json.loads(obj["Body"].read()))
            except Exception as e:
                logger.warning(f"Failed to read task {key}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=_LIST_PARALLELISM) as pool:
            results = list(pool.map(_fetch, keys))
        return [t for t in results if t is not None]


def _task_key(task_id: str) -> str:
    return f"{_TASKS_PREFIX}{task_id}.json"


def _slack_index_key(thread_ts: str) -> str:
    return f"{_INDEX_SLACK_PREFIX}{thread_ts}.json"
