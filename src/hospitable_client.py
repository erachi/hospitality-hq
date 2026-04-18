"""Client for the Hospitable API."""

import requests
from datetime import datetime, timedelta
from config import get_hospitable_token, HOSPITABLE_BASE_URL, RESERVATION_LOOKBACK_DAYS, RESERVATION_LOOKAHEAD_DAYS


class HospitableClient:
    """Thin wrapper around the Hospitable public API."""

    def __init__(self):
        self.base_url = HOSPITABLE_BASE_URL
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {get_hospitable_token()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _get(self, path: str, params: dict = None) -> dict:
        """Make a GET request to the Hospitable API."""
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_properties(self) -> list[dict]:
        """Fetch all properties for the authenticated user."""
        data = self._get("/properties")
        return data.get("data", [])

    def get_property(self, uuid: str) -> dict:
        """Fetch a single property with details."""
        data = self._get(f"/properties/{uuid}", params={"include": "details"})
        return data.get("data", {})

    def get_active_reservations(self, property_uuids: list[str]) -> list[dict]:
        """Fetch reservations that are currently active or upcoming.

        Pulls accepted + checkpoint reservations with check-in from
        LOOKBACK days ago through LOOKAHEAD days ahead, including
        guest info for each.
        """
        today = datetime.utcnow().date()
        start = today - timedelta(days=RESERVATION_LOOKBACK_DAYS)
        end = today + timedelta(days=RESERVATION_LOOKAHEAD_DAYS)

        all_reservations = []

        for prop_uuid in property_uuids:
            page = 1
            while True:
                data = self._get(
                    "/reservations",
                    params={
                        "properties[]": prop_uuid,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "date_query": "checkin",
                        "status[]": ["accepted", "checkpoint"],
                        "include": "guest",
                        "per_page": 50,
                        "page": page,
                    },
                )
                reservations = data.get("data", [])
                # Hospitable's list-reservations endpoint doesn't return
                # property_id on reservations — attach it ourselves since we
                # already know which property we queried.
                for res in reservations:
                    res["property_id"] = prop_uuid
                all_reservations.extend(reservations)

                # Check if there are more pages
                if data.get("meta", {}).get("current_page", 1) >= data.get("meta", {}).get("last_page", 1):
                    break
                page += 1

        return all_reservations

    def get_reservation_messages(self, reservation_uuid: str) -> list[dict]:
        """Fetch all messages for a reservation thread."""
        data = self._get(
            f"/reservations/{reservation_uuid}/messages",
            params={"include": "metadata"},
        )
        return data.get("data", [])

    def get_property_knowledge_hub(self, property_uuid: str) -> dict:
        """Fetch the Knowledge Hub for a property."""
        data = self._get(f"/properties/{property_uuid}/knowledge-hub")
        return data.get("data", {})

    def get_reservation_detail(self, reservation_uuid: str) -> dict:
        """Fetch full reservation details including financials and property info.

        `include=properties` nests a `properties` array on the response with
        `id` and `name` — the detail endpoint does NOT return `property_id` or
        `property_name` at the top level, so we rely on this include to
        identify which property the reservation belongs to.
        """
        data = self._get(
            f"/reservations/{reservation_uuid}",
            params={"include": "guest,financials,properties"},
        )
        return data.get("data", {})
