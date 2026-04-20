"""S3-backed storage for expenses.

Layout:
    s3://<bucket>/
    ├── expenses/
    │   └── <year>/<id>.json              # full expense metadata
    ├── receipts/
    │   └── <year>/<id>.<ext>             # original image, Object Lock 7yr
    ├── thumbs/
    │   └── <year>/<id>.jpg               # 1024px preview for Slack
    ├── exports/
    │   └── <year>/<export_id>.zip        # 90-day lifecycle (v2)
    └── indexes/
        └── slack/<thread_ts>.json        # {"expense_id": "EXP-..."}

Chosen to mirror task_store.py: flat JSON-per-object, LIST+parallel-GET
for queries. S3 versioning is the free audit trail. Properties and
merchant patterns are bundled with the Lambda as seed/, not stored here.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from config import EXPENSES_BUCKET
from expense_models import Expense, EXPENSE_ID_RE


logger = logging.getLogger(__name__)

_EXPENSES_PREFIX = "expenses/"
_RECEIPTS_PREFIX = "receipts/"
_THUMBS_PREFIX = "thumbs/"
_EXPORTS_PREFIX = "exports/"
_INDEX_SLACK_PREFIX = "indexes/slack/"

# LIST+GET fan-out — plenty for our volume.
_LIST_PARALLELISM = 20


@lru_cache(maxsize=1)
def _s3():
    return boto3.client("s3")


class ExpenseStore:
    """Read/write expenses to S3."""

    def __init__(self, bucket: Optional[str] = None):
        self.bucket = bucket or EXPENSES_BUCKET

    # ─── Expense CRUD ────────────────────────────────────────────────────

    def get(self, expense_id: str) -> Optional[Expense]:
        m = EXPENSE_ID_RE.match(expense_id or "")
        if not m:
            return None
        year = m.group(1)
        try:
            obj = _s3().get_object(
                Bucket=self.bucket, Key=_expense_key(year, expense_id)
            )
            return Expense.from_dict(json.loads(obj["Body"].read()))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, expense: Expense) -> None:
        expense.touch()
        body = json.dumps(expense.to_dict(), indent=2).encode("utf-8")
        _s3().put_object(
            Bucket=self.bucket,
            Key=_expense_key(expense.year(), expense.id),
            Body=body,
            ContentType="application/json",
        )

    def list_for_year(self, year: str) -> list[Expense]:
        keys = self._list_keys(f"{_EXPENSES_PREFIX}{year}/")
        return self._get_many(keys)

    def list_expense_keys_for_year(self, year: str) -> list[str]:
        return self._list_keys(f"{_EXPENSES_PREFIX}{year}/")

    # ─── ID generation ───────────────────────────────────────────────────

    def next_id(self, year: str) -> str:
        """Next EXP-YYYY-NNNN id for the year.

        Race-safe enough at our volume: at <500 receipts/yr, the window
        for two concurrent puts to pick the same sequence number is tiny
        and the monthly summary would surface any silent overwrite. If
        the race becomes real, swap to a counter object with
        If-Match conditional puts.
        """
        n = len(self.list_expense_keys_for_year(year)) + 1
        return f"EXP-{year}-{n:04d}"

    # ─── Image storage ───────────────────────────────────────────────────

    def put_receipt_image(
        self,
        year: str,
        expense_id: str,
        body: bytes,
        content_type: str,
        retention_until: Optional[str] = None,
    ) -> str:
        """Upload the original receipt. Returns the S3 key.

        retention_until: ISO-8601 datetime string. When provided and the
        bucket has Object Lock enabled, applies GOVERNANCE retention.
        """
        ext = _guess_ext(content_type)
        key = f"{_RECEIPTS_PREFIX}{year}/{expense_id}.{ext}"
        kwargs = dict(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
        if retention_until:
            kwargs["ObjectLockMode"] = "GOVERNANCE"
            kwargs["ObjectLockRetainUntilDate"] = retention_until
        _s3().put_object(**kwargs)
        return key

    def put_thumbnail(self, year: str, expense_id: str, body: bytes) -> str:
        key = f"{_THUMBS_PREFIX}{year}/{expense_id}.jpg"
        _s3().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="image/jpeg",
        )
        return key

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    # ─── Slack thread pointer index ──────────────────────────────────────

    def put_slack_index(self, thread_ts: str, expense_id: str) -> None:
        if not thread_ts or not expense_id:
            return
        _s3().put_object(
            Bucket=self.bucket,
            Key=_slack_index_key(thread_ts),
            Body=json.dumps({"expense_id": expense_id}).encode("utf-8"),
            ContentType="application/json",
        )

    def get_expense_by_thread(self, thread_ts: str) -> Optional[Expense]:
        if not thread_ts:
            return None
        try:
            obj = _s3().get_object(
                Bucket=self.bucket, Key=_slack_index_key(thread_ts)
            )
            pointer = json.loads(obj["Body"].read())
            return self.get(pointer.get("expense_id", ""))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    # ─── Internals ───────────────────────────────────────────────────────

    def _list_keys(self, prefix: str) -> list[str]:
        keys = []
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                keys.append(key)
        return keys

    def _get_many(self, keys: list[str]) -> list[Expense]:
        def _fetch(key: str) -> Optional[Expense]:
            try:
                obj = _s3().get_object(Bucket=self.bucket, Key=key)
                return Expense.from_dict(json.loads(obj["Body"].read()))
            except Exception as e:
                logger.warning(f"Failed to read expense {key}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=_LIST_PARALLELISM) as pool:
            results = list(pool.map(_fetch, keys))
        return [e for e in results if e is not None]


def _expense_key(year: str, expense_id: str) -> str:
    return f"{_EXPENSES_PREFIX}{year}/{expense_id}.json"


def _slack_index_key(thread_ts: str) -> str:
    return f"{_INDEX_SLACK_PREFIX}{thread_ts}.json"


def _guess_ext(content_type: str) -> str:
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/heic": "heic",
        "image/webp": "webp",
    }.get((content_type or "").lower(), "bin")
