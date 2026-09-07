"""Guardrail tests.

The engine's risk is not that it misses a burnt inbox; it is that a false
positive pulls working mailboxes out of a live campaign. These tests pin the
limits that make it safe to leave running unattended.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import health as H
from engine.config import Config
from engine.policy import BENCH, ESCALATE, PAUSE_CAMPAIGN, REPLACE, RETIRE, decide


@pytest.fixture
def cfg():
    return Config.load()


def mailbox(mailbox_id, domain, reputation=100.0, verdict=H.HEALTHY, sent=200):
    return H.MailboxReading(
        mailbox_id=mailbox_id, email=f"u{mailbox_id}@{domain}", domain=domain,
        verdict=verdict, reputation=reputation, warmup_sent=sent,
        reasons=[f"warm-up reputation {reputation:.0f}%"] if verdict == H.BURNING else [],
    )


def domain(name, verdict=H.HEALTHY, ids=(), reasons=(), **kw):
    return H.DomainReading(domain=name, verdict=verdict, mailbox_ids=list(ids),
                           reasons=list(reasons), **kw)


def campaign(sent=5000, replies=100, bounces=50, campaign_id=1):
    rates = H.campaign_rates({"sent_count": sent, "unique_sent_count": sent,
                              "reply_count": replies, "bounce_count": bounces})
    return {"id": campaign_id, "name": "test", "rates": rates}


def test_bounce_spike_stops_rotation_entirely(cfg):
    """A list fault must not trigger a rotation: fresh domains would burn too."""
    boxes = [mailbox(i, "a.com", 80.0, H.BURNING) for i in range(1, 7)]
    plan = decide(campaign(sent=5000, bounces=500), boxes,
                  {"a.com": domain("a.com", ids=[b.mailbox_id for b in boxes])},
                  {}, reserve_count=20, cfg=cfg)

    assert [a.action for a in plan.of(PAUSE_CAMPAIGN)]
    assert not plan.of(BENCH), "benched mailboxes despite a list-quality fault"
    assert not plan.of(REPLACE), "ordered replacements despite a list-quality fault"


def test_blast_radius_caps_benches(cfg):
    """One run may never remove more than the configured share of capacity."""
    boxes = [mailbox(i, "a.com", 70.0, H.BURNING) for i in range(1, 11)]
    plan = decide(campaign(), boxes,
                  {"a.com": domain("a.com", ids=[b.mailbox_id for b in boxes])},
                  {}, reserve_count=50, cfg=cfg)

    allowed = int(10 * cfg.rotation["max_bench_fraction"])
    assert len(plan.of(BENCH)) == allowed
    assert len(plan.of(ESCALATE)) >= 10 - allowed


def test_never_drops_below_minimum_senders(cfg):
    """A small campaign is escalated rather than emptied."""
    boxes = [mailbox(i, "a.com", 70.0, H.BURNING) for i in range(1, 4)]
    plan = decide(campaign(), boxes,
                  {"a.com": domain("a.com", ids=[b.mailbox_id for b in boxes])},
                  {}, reserve_count=10, cfg=cfg)

    assert not plan.of(BENCH)
    assert plan.of(ESCALATE)


def test_listed_domain_is_replaced_not_benched(cfg):
    """A listing costs part of the reply rate, not all of it.

    Pulling it before a replacement is warm removes capacity for nothing, so
    the domain keeps sending while its successor warms.
    """
    boxes = [mailbox(i, "a.com") for i in range(1, 5)]
    plan = decide(campaign(), boxes,
                  {"a.com": domain("a.com", H.LISTED, [b.mailbox_id for b in boxes],
                                   ["listed on SURBL multi"])},
                  {}, reserve_count=10, cfg=cfg)

    assert len(plan.of(REPLACE)) == 1
    assert not plan.of(BENCH)
    assert "retire_on" in plan.of(REPLACE)[0].payload


def test_unauthenticated_domain_stops_sending_now(cfg):
    """Missing DKIM is the one domain fault that benches immediately."""
    boxes = [mailbox(i, "a.com") for i in range(1, 9)]
    plan = decide(campaign(), boxes,
                  {"a.com": domain("a.com", H.MISCONFIGURED,
                                   [b.mailbox_id for b in boxes], ["missing DKIM"])},
                  {}, reserve_count=10, cfg=cfg)

    benches = plan.of(BENCH)
    assert benches
    assert all("missing DKIM" in b.reason for b in benches), \
        "bench reason must name the domain fault, not the mailbox verdict"


def test_burnt_mailbox_is_retired_not_rested(cfg):
    """GEX: burnt inboxes are replaced, never rehabilitated."""
    boxes = [mailbox(i, "a.com") for i in range(1, 8)]
    boxes[0] = mailbox(1, "a.com", 80.0, H.BURNING)
    plan = decide(campaign(), boxes,
                  {"a.com": domain("a.com", ids=[b.mailbox_id for b in boxes])},
                  {}, reserve_count=10, cfg=cfg)

    assert [a.mailbox_id for a in plan.of(RETIRE)] == [1]


def test_disconnected_mailbox_is_benched_without_reputation(cfg):
    """A mailbox that cannot authenticate is pulled on its own evidence."""
    boxes = [mailbox(i, "a.com") for i in range(1, 8)]
    boxes[0] = H.MailboxReading(mailbox_id=1, email="u1@a.com", domain="a.com",
                                verdict=H.BROKEN, smtp_ok=False,
                                reasons=["SMTP disconnected"])
    plan = decide(campaign(), boxes,
                  {"a.com": domain("a.com", ids=[b.mailbox_id for b in boxes])},
                  {}, reserve_count=10, cfg=cfg)

    assert [a.mailbox_id for a in plan.of(BENCH)] == [1]
    assert not plan.of(RETIRE), "a disconnection is not a burn"


def test_thin_reserve_is_reported(cfg):
    """Benching without a warm replacement is capacity loss; say so."""
    boxes = [mailbox(i, "a.com") for i in range(1, 8)]
    boxes[0] = mailbox(1, "a.com", 80.0, H.BURNING)
    plan = decide(campaign(), boxes,
                  {"a.com": domain("a.com", ids=[b.mailbox_id for b in boxes])},
                  {}, reserve_count=0, cfg=cfg)

    assert any("replacements available" in a.reason for a in plan.of(ESCALATE))


def test_replacements_are_capped_terminal_first(cfg):
    """Ordering domains costs money, so a run takes a bounded number."""
    domains, boxes = {}, []
    for index, name in enumerate(["a.com", "b.com", "c.com", "d.com", "e.com"]):
        ids = [index * 2 + 1, index * 2 + 2]
        boxes += [mailbox(i, name) for i in ids]
        verdict = H.LISTED if name in ("d.com", "e.com") else H.DEGRADED
        domains[name] = domain(name, verdict, ids, [f"{verdict} fault"])

    plan = decide(campaign(), boxes, domains, {}, reserve_count=50, cfg=cfg)

    replaced = [a.target for a in plan.of(REPLACE)]
    assert len(replaced) == cfg.rotation["max_replacements_per_run"]
    assert {"d.com", "e.com"} <= set(replaced), "terminal faults must be ordered first"
