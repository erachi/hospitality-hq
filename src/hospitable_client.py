"""Client for the Hospitable API."""

from datetime import datetime, timedelta

import requests

from config import (
    HOSPITABLE_BASE_URL,
    RESERVATION_LOOKAHEAD_DAYS,
    RESERVATION_LOOKBACK_DAYS,
    get_hospitable_token,
)


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

    def _post(self, path: str, json_body: dict) -> dict:
        """Make a POST request to the Hospitable API."""
        url = f"{self.base_url}{path}"
        resp = self.session.post(url, json=json_body)
        resp.raise_for_status()
        return resp.json()

    def send_reservation_message(self, reservation_uuid: str, body: str) -> dict:
        """Send a host message to a reservation thread.

        Posts to `POST /reservations/{uuid}/messages` with a plain-text
        body. The caller (the stay-channel "Send to guest" button) is
        responsible for ensuring a human approved this. Returns the
        raw Hospitable response.
        """
        return self._post(
            f"/reservations/{reservation_uuid}/messages",
            {"body": body},
        )

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
                if data.get("meta", {}).get("current_page", 1) >= data.get("meta", {}).get(
                    "last_page", 1
                ):
                    break
                page += 1

        return all_reservations

    def get_recent_inquiries(self, property_uuids: list[str], since: datetime) -> list[dict]:
        """Fetch inquiries (pre-booking conversations) with thread activity since `since`.

        Inquiries are a separate resource from reservations — a guest asking
        about a stay before booking never appears under /reservations.
        `since` must be UTC; the API wants "Y-m-d H:i:s".
        """
        last_message_at = since.strftime("%Y-%m-%d %H:%M:%S")
        all_inquiries = []

        for prop_uuid in property_uuids:
            page = 1
            while True:
                data = self._get(
                    "/inquiries",
                    params={
                        "properties[]": prop_uuid,
                        "last_message_at": last_message_at,
                        "include": "guest",
                        "per_page": 100,
                        "page": page,
                    },
                )
                inquiries = data.get("data", [])
                for inq in inquiries:
                    inq["property_id"] = prop_uuid
                all_inquiries.extend(inquiries)

                if data.get("meta", {}).get("current_page", 1) >= data.get("meta", {}).get(
                    "last_page", 1
                ):
                    break
                page += 1

        return all_inquiries

    def get_inquiry_detail(self, inquiry_uuid: str) -> dict:
        """Fetch an inquiry with guest, property, and its message thread.

        Messages come back nested under `messages`, newest first; callers
        should use get_inquiry_messages() for a chronological list.
        `properties` is a single object here (not an array as on reservations).
        """
        data = self._get(
            f"/inquiries/{inquiry_uuid}",
            params={"include": "guest,properties,messages"},
        )
        return data.get("data", {})

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
