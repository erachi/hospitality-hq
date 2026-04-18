"""One-shot debug helper: print the full Hospitable reservation detail JSON.

Usage:
    python scripts/debug_reservation.py <reservation_uuid>

Credentials come from SSM (via src/config.py) or from HOSPITABLE_API_TOKEN
in the environment. Run locally — not in Lambda.

Purpose: we saw "Unknown Property" + blank dates in Slack alerts. This prints
the raw JSON so we can see the real field names on the Hospitable response
(e.g. check_in vs checkin, whether property_id/property_name are present).
"""

import json
import sys
from pathlib import Path

# Make src/ importable.
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from hospitable_client import HospitableClient  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/debug_reservation.py <reservation_uuid>", file=sys.stderr)
        return 2

    reservation_uuid = sys.argv[1]
    client = HospitableClient()

    detail = client.get_reservation_detail(reservation_uuid)
    print("=== get_reservation_detail ===")
    print(json.dumps(detail, indent=2, default=str))

    print()
    print("=== top-level keys ===")
    print(sorted(detail.keys()))

    # Also dump a sample list-endpoint entry for the same property. The list and
    # detail endpoints don't always share field names, and the poll handler only
    # hits the list endpoint.
    from config import PROPERTY_UUIDS  # noqa: E402

    print()
    print("=== get_active_reservations sample (first entry per property) ===")
    reservations = client.get_active_reservations(PROPERTY_UUIDS)
    if reservations:
        sample = reservations[0]
        print(json.dumps(sample, indent=2, default=str))
        print()
        print("=== list-entry top-level keys ===")
        print(sorted(sample.keys()))
    else:
        print("(no active reservations)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
