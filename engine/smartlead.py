"""Smartlead API client.

Endpoint provenance, because it matters when this runs unattended:
  documented in Smartlead's public index (api.smartlead.ai/llms.txt)
    GET    /campaigns/
    GET    /campaigns/{id}/analytics
    PATCH  /campaigns/{id}/status
    GET    /email-accounts
    PATCH  /email-accounts/{id}
    GET    /email-accounts/{id}/warmup-stats
    POST   /campaigns/{id}/email-accounts
  present in the API but absent from that index
    GET    /campaigns/{id}/email-accounts
    DELETE /campaigns/{id}/email-accounts

The second group is exercised by Smartlead's own tooling but is not in the
published list, so a 404 there is treated as a contract change and raised
loudly rather than swallowed. Never let a rotation silently no-op.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger("smartlead")


class ReadOnlyViolation(RuntimeError):
    """A mutating call was attempted on a client opened in read-only mode."""


class SmartleadError(RuntimeError):
    pass


class SmartleadClient:
    """Thin, explicit wrapper. No pagination magic beyond what is needed.

    `read_only=True` makes every mutating method raise before it reaches the
    network, so a plan run cannot change production even if the calling code
    has a bug.
    """

    # Smartlead documents roughly 10 requests per 2 seconds.
    _min_interval = 0.22

    def __init__(self, api_key: str, base_url: str, read_only: bool = True, timeout: int = 30):
        if not api_key:
            raise SmartleadError("SMARTLEAD_API_KEY is empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.read_only = read_only
        self.timeout = timeout
        self.session = requests.Session()
        self._last_call = 0.0

    # -- transport --------------------------------------------------------

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_call
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None, attempts: int = 4) -> Any:
        if method != "GET" and self.read_only:
            raise ReadOnlyViolation(f"{method} {path} blocked: client is read-only")

        url = f"{self.base_url}{path}"
        params = {**(params or {}), "api_key": self.api_key}

        for attempt in range(1, attempts + 1):
            self._throttle()
            resp = self.session.request(
                method, url, params=params, json=json, timeout=self.timeout
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == attempts:
                    raise SmartleadError(
                        f"{method} {path} failed after {attempts} attempts: "
                        f"{resp.status_code} {resp.text[:200]}"
                    )
                backoff = 2 ** attempt
                log.warning("%s %s -> %s, retrying in %ss", method, path, resp.status_code, backoff)
                time.sleep(backoff)
                continue
            if resp.status_code == 401:
                raise SmartleadError("Smartlead rejected the API key (401)")
            if resp.status_code == 404:
                raise SmartleadError(
                    f"{method} {path} returned 404. If this is a campaign "
                    "email-account route the API contract has changed; stop and "
                    "check before rotating anything."
                )
            if resp.status_code >= 400:
                raise SmartleadError(f"{method} {path} -> {resp.status_code} {resp.text[:300]}")
            if not resp.content:
                return {}
            return resp.json()

    # -- reads ------------------------------------------------------------

    def campaigns(self) -> list[dict]:
        data = self._request("GET", "/campaigns/")
        return data if isinstance(data, list) else data.get("data", [])

    def active_campaigns(self) -> list[dict]:
        return [c for c in self.campaigns() if str(c.get("status", "")).upper() == "ACTIVE"]

    def campaign_analytics(self, campaign_id: int) -> dict:
        return self._request("GET", f"/campaigns/{campaign_id}/analytics")

    def email_accounts(self, limit: int = 100) -> list[dict]:
        """Every sending account on the workspace, paged to exhaustion."""
        out: list[dict] = []
        offset = 0
        while True:
            page = self._request(
                "GET", "/email-accounts/", params={"offset": offset, "limit": limit}
            )
            rows = page if isinstance(page, list) else page.get("data", [])
            out.extend(rows)
            if len(rows) < limit:
                return out
            offset += limit

    def campaign_email_accounts(self, campaign_id: int) -> list[dict]:
        data = self._request("GET", f"/campaigns/{campaign_id}/email-accounts")
        return data if isinstance(data, list) else data.get("data", [])

    def campaign_mailbox_stats(self, campaign_id: int) -> list[dict]:
        """Per-sending-account counts for a campaign.

        Not in the published index. When it is unavailable the caller falls
        back to apportioning campaign totals, and marks the result estimated
        so the policy will not condemn a domain on a number it inferred.
        """
        data = self._request("GET", f"/campaigns/{campaign_id}/statistics")
        if isinstance(data, dict):
            data = data.get("data") or data.get("statistics") or []
        return data if isinstance(data, list) else []

    def warmup_stats(self, email_account_id: int) -> dict:
        return self._request("GET", f"/email-accounts/{email_account_id}/warmup-stats")

    # -- writes -----------------------------------------------------------

    def set_daily_limit(self, email_account_id: int, max_per_day: int) -> Any:
        return self._request(
            "PATCH", f"/email-accounts/{email_account_id}",
            json={"max_email_per_day": max_per_day},
        )

    def add_to_campaign(self, campaign_id: int, email_account_ids: list[int]) -> Any:
        return self._request(
            "POST", f"/campaigns/{campaign_id}/email-accounts",
            json={"email_account_ids": email_account_ids},
        )

    def remove_from_campaign(self, campaign_id: int, email_account_ids: list[int]) -> Any:
        return self._request(
            "DELETE", f"/campaigns/{campaign_id}/email-accounts",
            json={"email_account_ids": email_account_ids},
        )

    def pause_campaign(self, campaign_id: int) -> Any:
        return self._request(
            "PATCH", f"/campaigns/{campaign_id}/status", json={"status": "PAUSED"}
        )
