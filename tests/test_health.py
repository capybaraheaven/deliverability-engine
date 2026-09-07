"""Scoring tests: the thresholds and the refusals to guess."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import health as H
from engine.config import Config


@pytest.fixture
def cfg():
    return Config.load()


def account(**kw):
    base = {"id": 1, "from_email": "u@a.com", "is_smtp_success": True,
            "is_imap_success": True}
    base.update(kw)
    return base


def warmup(sent=200, spam=2, reputation=100):
    return {"total_sent_count": sent, "total_spam_count": spam,
            "warmup_reputation": f"{reputation}%"}


def test_reputation_below_98_is_burning(cfg):
    reading = H.score_mailbox(account(), warmup(reputation=97), cfg)
    assert reading.verdict == H.BURNING


def test_reputation_at_98_is_not_burning(cfg):
    assert H.score_mailbox(account(), warmup(reputation=98), cfg).verdict != H.BURNING


def test_low_volume_mailbox_is_unknown_not_condemned(cfg):
    """A new mailbox has no trustworthy reading and must not be benched."""
    reading = H.score_mailbox(account(), warmup(sent=5, reputation=50), cfg)
    assert reading.verdict == H.UNKNOWN


def test_disconnection_outranks_reputation(cfg):
    reading = H.score_mailbox(account(is_smtp_success=False), warmup(reputation=100), cfg)
    assert reading.verdict == H.BROKEN


def test_missing_dkim_outranks_a_listing(cfg):
    """Both are terminal; the reason line should name the one that decides."""
    reading = H.score_domain("a.com", {
        "dnsRecords": {"spfRecord": True, "dkimRecords": False, "dmarcRecords": True},
        "blacklistStatus": {"isBlacklisted": True, "deepCheckProviders": ["SURBL multi"]},
    }, [], None, cfg)
    assert reading.verdict == H.MISCONFIGURED
    assert "DKIM" in reading.reasons[0]


def test_estimated_rates_never_condemn_a_domain(cfg):
    """Apportioned rates are identical across a campaign's domains.

    Acting on them would flag every domain or none, so they are reported and
    otherwise ignored.
    """
    zap = {"dnsRecords": {"spfRecord": True, "dkimRecords": True, "dmarcRecords": True},
           "blacklistStatus": {"isBlacklisted": False}}
    stats = {"sent": 5000, "replies": 5, "bounces": 400}

    measured = H.score_domain("a.com", zap, [], stats, cfg, estimated_rates=False)
    estimated = H.score_domain("a.com", zap, [], stats, cfg, estimated_rates=True)

    assert measured.verdict == H.DEGRADED
    assert estimated.verdict == H.HEALTHY
    assert estimated.bounce_rate is not None, "still reported, just not acted on"


def test_worst_decile_needs_a_pool_to_rank(cfg):
    """Ranking four domains produces noise, not a signal."""
    few = [H.DomainReading(domain=f"{i}.com", verdict=H.HEALTHY, reply_rate=0.02,
                           sends=500) for i in range(4)]
    H.mark_worst_by_reply(few, cfg)
    assert all(r.verdict == H.HEALTHY for r in few)

    many = [H.DomainReading(domain=f"{i}.com", verdict=H.HEALTHY,
                            reply_rate=0.001 * (i + 1), sends=500) for i in range(10)]
    H.mark_worst_by_reply(many, cfg)
    assert many[0].verdict == H.DEGRADED, "the worst domain in a real pool is demoted"
