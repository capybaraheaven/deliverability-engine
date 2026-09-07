"""Zapmail API client: domain inventory, infrastructure health, and the
mailbox export that pushes replacements into Smartlead.

Zapmail carries the signals Smartlead cannot see. Smartlead reports how a
mailbox is performing; Zapmail reports whether the domain underneath it is
authenticated and whether it has been listed. A domain on SURBL will keep
posting acceptable Smartlead numbers for a while before the reply rate falls
off, which is exactly the window worth acting in.

Zapmail publishes no complete spec, so every path comes from config.yaml and
`verify()` probes them before a run is allowed to act.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger("zapmail")


class ZapmailError(RuntimeError):
    pass


class ReadOnlyViolation(RuntimeError):
    pass


class ZapmailClient:
    _min_interval = 0.3

    def __init__(self, api_key: str, base_url: str, paths: dict[str, str],
                 workspace_key: str | None = None, read_only: bool = True,
                 timeout: int = 45):
        if not api_key:
            raise ZapmailError("ZAPMAIL_API_KEY is empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.paths = paths
        self.workspace_key = workspace_key
        self.read_only = read_only
        self.timeout = timeout
        self.session = requests.Session()
        self._last_call = 0.0

    # -- transport --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        if self.workspace_key:
            headers["workspaceKey"] = self.workspace_key
        return headers

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None, attempts: int = 3) -> Any:
        if method != "GET" and self.read_only:
            raise ReadOnlyViolation(f"{method} {path} blocked: client is read-only")

        url = f"{self.base_url}{path}"
        for attempt in range(1, attempts + 1):
            delta = time.monotonic() - self._last_call
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last_call = time.monotonic()

            resp = self.session.request(
                method, url, params=params, json=json,
                headers=self._headers(), timeout=self.timeout,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == attempts:
                    raise ZapmailError(f"{method} {path} -> {resp.status_code}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                raise ZapmailError("Zapmail rejected the API key (401)")
            if resp.status_code >= 400:
                raise ZapmailError(f"{method} {path} -> {resp.status_code} {resp.text[:300]}")
            return resp.json() if resp.content else {}

    # -- preflight --------------------------------------------------------

    def verify(self) -> dict[str, str]:
        """Probe every configured GET path. Returns {name: 'ok' | error}.

        Run before acting. A path that has moved would otherwise surface as an
        empty domain list, which reads as "everything is healthy" and is the
        most dangerous possible failure for this program.
        """
        results: dict[str, str] = {}
        for name in ("workspaces", "domains", "mailboxes"):
            path = self.paths.get(name)
            if not path:
                results[name] = "not configured"
                continue
            try:
                self._request("GET", path, params={"limit": 1})
                results[name] = "ok"
            except ZapmailError as exc:
                results[name] = str(exc)
        return results

    # -- reads ------------------------------------------------------------

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """Zapmail nests most collections under data; some routes do not."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def workspaces(self) -> list[dict]:
        data = self._unwrap(self._request("GET", self.paths["workspaces"]))
        if isinstance(data, dict):
            return data.get("workspaces", [])
        return data or []

    def domains_with_mailboxes(self, limit: int = 50) -> list[dict]:
        """Every domain and the mailboxes on it, paged to exhaustion.

        Grouping matters: mailboxes on one domain share its reputation, so the
        engine judges them as a unit.
        """
        out: list[dict] = []
        page = 1
        while True:
            payload = self._unwrap(
                self._request("GET", self.paths["mailboxes"],
                              params={"page": page, "limit": limit})
            )
            domains = (payload or {}).get("domains", []) if isinstance(payload, dict) else []
            out.extend(domains)
            total_pages = (payload or {}).get("totalPages", 1) if isinstance(payload, dict) else 1
            if page >= (total_pages or 1) or not domains:
                return out
            page += 1

    def domain_health(self, domain_id: str) -> dict:
        """DNS correctness, blacklist listings, and nameserver reputation."""
        return self._unwrap(
            self._request("GET", self.paths["domain_health"], params={"domainId": domain_id})
        )

    def prewarmed_domains(self) -> list[dict]:
        """The reserve pool: warmed domains available to rotate in."""
        try:
            data = self._unwrap(self._request("GET", self.paths["prewarmed"]))
        except ZapmailError as exc:
            log.warning("prewarmed pool unavailable: %s", exc)
            return []
        if isinstance(data, dict):
            return data.get("domains", []) or data.get("prewarmedDomains", [])
        return data or []

    # -- writes -----------------------------------------------------------

    def export_to_smartlead(self, app: str, *, mailbox_ids: list[str] | None = None,
                            domain_contains: str | None = None) -> Any:
        """Push mailboxes into the connected sending platform.

        Zapmail holds the platform credential, so the engine never handles a
        mailbox password to complete a rotation.
        """
        body: dict[str, Any] = {"apps": [app]}
        if mailbox_ids:
            body["ids"] = mailbox_ids
        if domain_contains:
            body["contains"] = domain_contains
        return self._request("POST", self.paths["export"], json=body)
