"""Decide what to do about the readings.

Also pure. `decide()` returns a plan; nothing here calls an API, which is why
a plan run and an apply run make identical decisions and only differ in
whether the plan is executed.

The guardrails are the point. An unattended program that can pull mailboxes
out of live campaigns needs to be more afraid of its own false positives than
of missing a burnt inbox, so:

  circuit breaker   A bounce spike is a list fault. Above the threshold the
                    engine stops rotating that campaign entirely, because
                    feeding warm domains to a bad list burns them too.
  blast radius      No run may remove more than a set share of a campaign's
                    capacity. Wanting to exceed it is evidence of a systemic
                    fault, which is a human's problem.
  floor             A campaign never drops below a minimum number of senders.
  insurance         Benching without a warm replacement is capacity loss, so
                    a thin reserve is reported before it becomes a surprise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from engine.health import (
    BROKEN, BURNING, DEGRADED, LISTED, MISCONFIGURED,
    DomainReading, MailboxReading,
)

PAUSE_CAMPAIGN = "pause_campaign"
BENCH = "bench"
RETIRE = "retire"
REPLACE = "replace"
THROTTLE = "throttle"
RAMP = "ramp"
ESCALATE = "escalate"


@dataclass
class Action:
    action: str
    target: str
    reason: str
    campaign_id: int | None = None
    mailbox_id: int | None = None
    payload: dict = field(default_factory=dict)

    def describe(self) -> str:
        scope = f" [campaign {self.campaign_id}]" if self.campaign_id else ""
        return f"{self.action.upper():<15} {self.target}{scope} — {self.reason}"


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, action: Action) -> None:
        self.actions.append(action)

    def of(self, kind: str) -> list[Action]:
        return [a for a in self.actions if a.action == kind]

    @property
    def mutating(self) -> list[Action]:
        """Actions that change something. Escalations are reports."""
        return [a for a in self.actions if a.action != ESCALATE]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _severity(reading: MailboxReading) -> tuple:
    """Worst first: broken before burning, then lowest reputation."""
    rank = {BROKEN: 0, BURNING: 1}.get(reading.verdict, 2)
    return (rank, reading.reputation if reading.reputation is not None else 999)


def decide(campaign, mailboxes, domains, states, reserve_count, cfg) -> Plan:
    """Build the plan for one campaign.

    campaign      dict with id, name, and the normalised rates from
                  health.campaign_rates
    mailboxes     MailboxReading for every account on the campaign
    domains       DomainReading keyed by domain name
    states        current mailbox_state rows, keyed by mailbox id
    reserve_count warm mailboxes available to rotate in
    """
    plan = Plan()
    rot, camp = cfg.rotation, cfg.campaign
    campaign_id = campaign["id"]
    rates = campaign["rates"]

    # 1. Circuit breaker. Checked before anything else so a list problem never
    #    triggers a rotation.
    bounce = rates.get("bounce_rate")
    if (rates.get("sent", 0) >= camp["min_sends_for_bounce_check"]
            and bounce is not None and bounce > camp["bounce_rate_circuit_breaker"]):
        plan.add(Action(
            PAUSE_CAMPAIGN, campaign.get("name", str(campaign_id)),
            f"bounce rate {bounce:.2%} above {camp['bounce_rate_circuit_breaker']:.0%} "
            f"over {rates['sent']} sends — list quality, not infrastructure",
            campaign_id=campaign_id,
        ))
        plan.add(Action(
            ESCALATE, campaign.get("name", str(campaign_id)),
            "verify the lead list before any rotation; replacing domains now "
            "would burn the replacements on the same list",
            campaign_id=campaign_id,
        ))
        return plan

    reply = rates.get("reply_rate")
    if reply is not None and reply < camp["reply_rate_floor"]:
        plan.add(Action(
            ESCALATE, campaign.get("name", str(campaign_id)),
            f"reply rate {reply:.2%} below {camp['reply_rate_floor']:.0%} — "
            "check copy and targeting alongside the infrastructure findings",
            campaign_id=campaign_id,
        ))

    # 2. Domain faults. A domain condemns every mailbox on it, because they
    #    share its reputation.
    condemned: dict[int, str] = {}
    replacements: list[tuple[int, Action]] = []
    for reading in domains.values():
        if not reading.mailbox_ids:
            continue
        reason = "; ".join(reading.reasons) or reading.verdict

        if reading.verdict == MISCONFIGURED:
            # Unauthenticated mail damages the domain with every send, so this
            # is the one domain fault that stops sending immediately.
            for mailbox_id in reading.mailbox_ids:
                condemned[mailbox_id] = f"{reading.domain}: {reason}"
            plan.add(Action(
                ESCALATE, reading.domain,
                f"{reason} — fix DNS before returning this domain to sending",
                campaign_id=campaign_id,
            ))

        elif reading.verdict == LISTED:
            # Deliberately not benched today. A listing costs roughly 30% of
            # reply rate, not all of it, and pulling it before a replacement
            # is warm removes capacity for nothing.
            retire_on = (_now() + timedelta(days=rot["retire_delay_days"])).date().isoformat()
            replacements.append((0, Action(
                REPLACE, reading.domain,
                f"{reason} — a listing does not lift, so warm a replacement now "
                f"and keep sending until {retire_on}",
                campaign_id=campaign_id,
                payload={"retire_on": retire_on, "mailbox_ids": reading.mailbox_ids},
            )))

        elif reading.verdict == DEGRADED:
            replacements.append((1, Action(
                REPLACE, reading.domain,
                f"{reason} — start a replacement warming",
                campaign_id=campaign_id,
                payload={"mailbox_ids": reading.mailbox_ids},
            )))

    # Ordering a replacement commits money and warm-up weeks, so a run takes a
    # bounded number: terminal faults first, then degraded.
    replacements.sort(key=lambda pair: pair[0])
    cap = rot["max_replacements_per_run"]
    for rank, (_priority, action) in enumerate(replacements):
        if rank < cap:
            plan.add(action)
        else:
            plan.add(Action(
                ESCALATE, action.target,
                f"{action.reason.split(' — ')[0]} — queued behind {cap} "
                "replacements already ordered this run",
                campaign_id=campaign_id,
            ))

    # 3. Mailbox faults.
    candidates = [
        m for m in mailboxes
        if (m.verdict in (BROKEN, BURNING) or m.mailbox_id in condemned)
        and states.get(m.mailbox_id, {}).get("status") != "benched"
    ]
    candidates.sort(key=_severity)

    active = [m for m in mailboxes if states.get(m.mailbox_id, {}).get("status") != "benched"]
    max_by_fraction = int(len(active) * rot["max_bench_fraction"])
    max_by_floor = max(0, len(active) - rot["min_active_mailboxes"])
    allowance = max(0, min(max_by_fraction, max_by_floor))

    for index, reading in enumerate(candidates):
        why = condemned.get(reading.mailbox_id) or "; ".join(reading.reasons) or reading.verdict
        if index >= allowance:
            plan.add(Action(
                ESCALATE, reading.email,
                f"{why} — held back: this run already benched {allowance} of "
                f"{len(active)} mailboxes on this campaign",
                campaign_id=campaign_id, mailbox_id=reading.mailbox_id,
            ))
            continue

        plan.add(Action(
            BENCH, reading.email, why,
            campaign_id=campaign_id, mailbox_id=reading.mailbox_id,
            payload={"domain": reading.domain},
        ))
        if reading.verdict == BURNING:
            # GEX: a burnt inbox is not rehabilitated. Retiring it here stops
            # a later run from cycling it back in.
            plan.add(Action(
                RETIRE, reading.email,
                "burnt mailboxes are replaced",
                campaign_id=campaign_id, mailbox_id=reading.mailbox_id,
                payload={"domain": reading.domain},
            ))

    # 4. Insurance. Benching without a warm replacement is capacity loss.
    benched = len(plan.of(BENCH))
    if benched:
        ratio = reserve_count / len(active) if active else 0
        if reserve_count < benched:
            plan.add(Action(
                ESCALATE, campaign.get("name", str(campaign_id)),
                f"{benched} mailboxes benched but only {reserve_count} warm "
                "replacements available — order more infrastructure",
                campaign_id=campaign_id,
            ))
        elif ratio < rot["min_insurance_ratio"]:
            plan.add(Action(
                ESCALATE, campaign.get("name", str(campaign_id)),
                f"warm reserve is {ratio:.0%} of active sending, below the "
                f"{rot['min_insurance_ratio']:.0%} floor",
                campaign_id=campaign_id,
            ))

    # 5. Ramp the replacements already in service. A warm mailbox handed full
    #    volume on day one burns the way the one it replaced did.
    for reading in mailboxes:
        state = states.get(reading.mailbox_id)
        if not state or state.get("status") != "active":
            continue
        cap = state.get("daily_cap")
        if cap is None or cap >= rot["ramp_target"] or reading.verdict != "healthy":
            continue
        plan.add(Action(
            RAMP, reading.email,
            f"clean run at {cap}/day, raising toward {rot['ramp_target']}",
            campaign_id=campaign_id, mailbox_id=reading.mailbox_id,
            payload={"from": cap, "to": min(cap + rot["ramp_step"], rot["ramp_target"])},
        ))

    return plan
