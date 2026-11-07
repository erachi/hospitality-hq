"""S3-backed storage for stay journals.

One JSON object per reservation at

    s3://<bucket>/<prefix>/<reservation_uuid>.json

S3 versioning on the bucket gives us the audit trail. Reads are a
single GET; writes are a single PUT of the full object. This is the
same shape as `task_store.TaskStore` and acceptable at our volume
(low hundreds of stays per year).

Free Slack drops messages older than ~90 days from both the UI and
conversations.history, so this store is the source of truth: agent
context assembly reads from here, not from Slack.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from config import STAY_JOURNAL_BUCKET, STAY_JOURNAL_PREFIX
from stay_models import StayEntry, StayJournal


logger = logging.getLogger(__name__)

# Parallelism for LIST+GET fan-out when scanning all journals. Matches
# TaskStore (task_store.py:41) — comfortably within Lambda capacity.
_LIST_PARALLELISM = 20


@lru_cache(maxsize=1)
def _s3():
    """Cached S3 client — reused across warm Lambda invocations."""
    return boto3.client("s3")


class StayStore:
    """Read/write stay journals to S3.

    Concurrency is last-writer-wins. Two writers racing on the same
    journal is unlikely in practice: webhook writes and agent writes
    are serialized per-reservation by Lambda event ordering, and we
    don't expect two humans to produce channel messages inside the
    same millisecond. If it becomes an issue, wire the ETag hook
    noted in task_store.py:51-54.
    """

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
    ):
        self.bucket = bucket or STAY_JOURNAL_BUCKET
        # Normalize to a trailing slash; empty means root.
        p = prefix if prefix is not None else STAY_JOURNAL_PREFIX
        self.prefix = p.rstrip("/") + "/" if p else ""

    # ─── Journal CRUD ────────────────────────────────────────────────

    def get(self, reservation_uuid: str) -> Optional[StayJournal]:
        """Fetch one journal by reservation_uuid. None if missing."""
        if not reservation_uuid:
            return None
        try:
            obj = _s3().get_object(
                Bucket=self.bucket, Key=self._key(reservation_uuid)
            )
            return StayJournal.from_dict(json.loads(obj["Body"].read()))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, journal: StayJournal) -> None:
        """Write a journal. Touches updated_at."""
        journal.touch()
        body = json.dumps(journal.to_dict(), indent=2).encode("utf-8")
        _s3().put_object(
            Bucket=self.bucket,
            Key=self._key(journal.reservation_uuid),
            Body=body,
            ContentType="application/json",
        )

    def append_entry(
        self, reservation_uuid: str, entry: StayEntry
    ) -> Optional[StayJournal]:
        """Load, append, write back. Returns the updated journal or None
        if the journal does not exist.

        The provisioner is responsible for seeding the journal before
        anything else can append to it — returning None here surfaces a
        missing-journal bug instead of silently creating a stub.
        """
        journal = self.get(reservation_uuid)
        if journal is None:
            logger.warning(
                "append_entry: no journal for reservation %s", reservation_uuid
            )
            return None
        journal.append(entry)
        self.put(journal)
        return journal

    def list_all(self) -> list[StayJournal]:
        """List every stored journal. Parallel GET fan-out."""
        keys = self._list_keys()
        if not keys:
            return []

        def _fetch(key: str) -> Optional[StayJournal]:
            try:
                obj = _s3().get_object(Bucket=self.bucket, Key=key)
                return StayJournal.from_dict(json.loads(obj["Body"].read()))
            except Exception as e:
                logger.warning("Failed to read journal %s: %s", key, e)
                return None

        with ThreadPoolExecutor(max_workers=_LIST_PARALLELISM) as pool:
            results = list(pool.map(_fetch, keys))
        return [j for j in results if j is not None]

    # ─── Internals ───────────────────────────────────────────────────

    def _key(self, reservation_uuid: str) -> str:
        return f"{self.prefix}{reservation_uuid}.json"

    def _list_keys(self) -> list[str]:
        keys = []
        paginator = _s3().get_paginator("list_objects_v2")
        prefix = self.prefix
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith("/") or not key.endswith(".json"):
                    continue
                keys.append(key)
        return keys
