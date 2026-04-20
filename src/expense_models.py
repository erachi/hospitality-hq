"""Data model for the expense-capture workflow.

One Expense = one receipt, stored as a single JSON object in S3 at
expenses/<year>/<id>.json. Allocations are nested so MVP (single
property at 100%) and v2 (multi-property splits) share the same
on-disk shape.

Money values are stored as strings (decimal-formatted) to avoid
float round-trip loss through JSON. Callers work with Decimal and
the helpers here normalize to 2dp on write.
"""

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


# EXP-YYYY-NNNN — year + 4-digit zero-padded sequence
EXPENSE_ID_RE = re.compile(r"^EXP-(\d{4})-(\d{4})$")

# OCR confidence levels. Stored on the Expense so the Slack card can
# visually flag low-confidence fields for human review.
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
ALL_CONFIDENCE = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)


@dataclass
class Allocation:
    """Allocation of a receipt total across properties.

    MVP always has exactly one Allocation at percent=100. v2 may split.
    """

    property_id: str
    percent: str    # "100.00"
    amount: str     # "311.97"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Allocation":
        return cls(
            property_id=data["property_id"],
            percent=data["percent"],
            amount=data["amount"],
        )

    @classmethod
    def single(cls, property_id: str, total) -> "Allocation":
        """One-property allocation at 100%."""
        return cls(
            property_id=property_id,
            percent="100.00",
            amount=money_str(total),
        )


@dataclass
class Expense:
    """A receipt + its extracted metadata."""

    id: str                       # "EXP-2026-0042"
    submitter_slack_id: str
    merchant_name: str
    transaction_date: str         # "YYYY-MM-DD" — date printed on the receipt
    total: str                    # decimal string, e.g. "311.97"
    currency: str
    image_s3_key: str
    image_sha256: str
    ocr_payload: dict
    ocr_model: str
    slack_channel_id: str
    slack_thread_ts: str
    created_at: str               # UTC ISO-8601 with trailing Z
    updated_at: str
    subtotal: Optional[str] = None
    tax: Optional[str] = None
    tip: Optional[str] = None
    category_id: Optional[str] = None
    property_id: Optional[str] = None
    payment_method: Optional[str] = None
    notes: Optional[str] = None
    thumbnail_s3_key: Optional[str] = None
    ocr_extraction_confidence: Optional[str] = None
    ocr_category_confidence: Optional[str] = None
    needs_review: bool = False
    review_reason: Optional[str] = None
    is_personal: bool = False
    allocations: list[Allocation] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["allocations"] = [a.to_dict() for a in self.allocations]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Expense":
        return cls(
            id=data["id"],
            submitter_slack_id=data["submitter_slack_id"],
            merchant_name=data["merchant_name"],
            transaction_date=data["transaction_date"],
            total=data["total"],
            currency=data.get("currency", "USD"),
            image_s3_key=data["image_s3_key"],
            image_sha256=data["image_sha256"],
            ocr_payload=data.get("ocr_payload") or {},
            ocr_model=data.get("ocr_model", ""),
            slack_channel_id=data["slack_channel_id"],
            slack_thread_ts=data["slack_thread_ts"],
            created_at=data["created_at"],
            updated_at=data.get("updated_at", data["created_at"]),
            subtotal=data.get("subtotal"),
            tax=data.get("tax"),
            tip=data.get("tip"),
            category_id=data.get("category_id"),
            property_id=data.get("property_id"),
            payment_method=data.get("payment_method"),
            notes=data.get("notes"),
            thumbnail_s3_key=data.get("thumbnail_s3_key"),
            ocr_extraction_confidence=data.get("ocr_extraction_confidence"),
            ocr_category_confidence=data.get("ocr_category_confidence"),
            needs_review=bool(data.get("needs_review", False)),
            review_reason=data.get("review_reason"),
            is_personal=bool(data.get("is_personal", False)),
            allocations=[Allocation.from_dict(a) for a in (data.get("allocations") or [])],
        )

    def touch(self) -> None:
        self.updated_at = now_iso()

    def year(self) -> str:
        m = EXPENSE_ID_RE.match(self.id)
        if not m:
            raise ValueError(f"expense id not in EXP-YYYY-NNNN form: {self.id}")
        return m.group(1)


def money_str(value) -> str:
    """Normalize a money value to a 2-decimal string."""
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"


def now_iso() -> str:
    """Current UTC time as ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def year_for_transaction(transaction_date: Optional[str] = None) -> str:
    """Year to file a new expense under.

    Prefers the receipt's printed date over the upload date — so a
    receipt dated 2026-12-31 uploaded on 2027-01-02 still files under 2026.
    """
    if transaction_date and len(transaction_date) >= 4:
        return transaction_date[:4]
    return datetime.now(timezone.utc).strftime("%Y")
