"""Execute a plan.

Every action is recorded to the store whether or not it ran, so a dry run
leaves the same audit trail as a live one and the two can be diffed.

A failure on one action never aborts the rest. Half a rotation applied and
reported beats a run that dies between removing a burnt mailbox and adding its
replacement, which would silently cost sending capacity.
"""

from __future__ import annotations

import logging

from engine.policy import (
    BENCH, ESCALATE, PAUSE_CAMPAIGN, RAMP, REPLACE, RETIRE, THROTTLE, Action, Plan,
)

log = logging.getLogger("actions")


class Executor:
    def __init__(self, smartlead, zapmail, store, cfg, run_id: str, apply: bool):
        self.sl = smartlead
        self.zm = zapmail
        self.store = store
        self.cfg = cfg
        self.run_id = run_id
        self.apply = apply

    def run(self, plan: Plan) -> dict:
        counts = {"applied": 0, "planned": 0, "failed": 0}
        for action in plan.actions:
            if action.action == ESCALATE:
                self.store.record_action(self.run_id, action.__dict__, applied=False)
                continue
            if not self.apply:
                self.store.record_action(self.run_id, action.__dict__, applied=False)
                counts["planned"] += 1
                continue
            try:
                self._execute(action)
                self.store.record_action(self.run_id, action.__dict__, applied=True)
                counts["applied"] += 1
            except Exception as exc:  # keep going; one failure is not the run
                log.error("%s failed: %s", action.describe(), exc)
                self.store.record_action(
                    self.run_id, action.__dict__, applied=False, error=str(exc)
                )
                counts["failed"] += 1
        return counts

    def _execute(self, action: Action) -> None:
        handler = {
            PAUSE_CAMPAIGN: self._pause,
            BENCH: self._bench,
            RETIRE: self._retire,
            REPLACE: self._replace,
            THROTTLE: self._throttle,
            RAMP: self._ramp,
        }[action.action]
        handler(action)

    # -- handlers ---------------------------------------------------------

    def _pause(self, action: Action) -> None:
        self.sl.pause_campaign(action.campaign_id)

    def _bench(self, action: Action) -> None:
        self.sl.remove_from_campaign(action.campaign_id, [action.mailbox_id])
        self.store.bench(
            action.mailbox_id, action.target, action.payload.get("domain", ""),
            self.cfg.rotation["rest_days"],
        )

    def _retire(self, action: Action) -> None:
        self.store.retire(
            action.mailbox_id, action.target,
            action.payload.get("domain", ""), action.reason,
        )

    def _replace(self, action: Action) -> None:
        """Push warm mailboxes for the failing domain's replacement into Smartlead.

        Zapmail holds the sending-platform credential and performs the import,
        so no mailbox password passes through this process. The new accounts
        are then added to the campaign at the ramp-start volume.
        """
        result = self.zm.export_to_smartlead(
            self.cfg.zapmail["export_app"],
            mailbox_ids=action.payload.get("replacement_mailbox_ids"),
            domain_contains=action.payload.get("replacement_domain"),
        )
        log.info("exported replacements for %s: %s", action.target, result)

        emails = {e.lower() for e in action.payload.get("replacement_emails", [])}
        if not emails:
            return
        ramp_start = self.cfg.rotation["ramp_start"]
        new_ids = [
            int(a["id"]) for a in self.sl.email_accounts()
            if (a.get("from_email") or "").lower() in emails
        ]
        for account_id in new_ids:
            self.sl.set_daily_limit(account_id, ramp_start)
        if new_ids:
            self.sl.add_to_campaign(action.campaign_id, new_ids)

    def _throttle(self, action: Action) -> None:
        self.sl.set_daily_limit(action.mailbox_id, action.payload["to"])
        self.store.upsert_state(
            action.mailbox_id, action.target, action.payload.get("domain", ""),
            daily_cap=action.payload["to"],
        )

    def _ramp(self, action: Action) -> None:
        self.sl.set_daily_limit(action.mailbox_id, action.payload["to"])
        self.store.upsert_state(
            action.mailbox_id, action.target, action.payload.get("domain", ""),
            daily_cap=action.payload["to"],
        )
