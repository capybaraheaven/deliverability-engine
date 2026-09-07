"""Turn raw API payloads into verdicts.

Pure functions. Nothing here performs I/O, which is what makes the thresholds
testable against fixtures and keeps a policy argument separate from an API
argument.

Three layers, because the three describe different faults and have different
remedies:

  mailbox   Smartlead warm-up reputation. The earliest signal available and
            the only one GEX runs fully unattended.
  domain    Reply and bounce rates aggregated across the mailboxes on a
            domain, plus Zapmail's DNS and blacklist state. Mailboxes on one
            domain share its reputation, so a domain fault condemns the group.
  campaign  Bounce rate across the whole campaign. A spike here is a list
            fault, and rotating fresh domains into a bad list burns them too.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Mailbox verdicts
HEALTHY = "healthy"
WATCH = "watch"
BURNING = "burning"
BROKEN = "broken"
UNKNOWN = "unknown"

# Domain verdicts
DEGRADED = "degraded"
MISCONFIGURED = "misconfigured"
LISTED = "listed"


@dataclass
class MailboxReading:
    mailbox_id: int
    email: str
    domain: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    reputation: float | None = None
    warmup_sent: int = 0
    spam_rate: float | None = None
    smtp_ok: bool = True
    imap_ok: bool = True
    campaign_id: int | None = None

    @property
    def actionable(self) -> bool:
        return self.verdict in (BURNING, BROKEN)


@dataclass
class DomainReading:
    domain: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    workspace: str | None = None
    domain_id: str | None = None
    score: int | None = None
    label: str | None = None
    blacklisted: bool = False
    blacklists: list[str] = field(default_factory=list)
    spf: bool = True
    dkim: bool = True
    dmarc: bool = True
    reply_rate: float | None = None
    bounce_rate: float | None = None
    sends: int = 0
    mean_reputation: float | None = None
    mailbox_ids: list[int] = field(default_factory=list)
    # True when reply/bounce were apportioned from campaign totals rather than
    # measured per domain. Estimated rates are reported but never condemn a
    # domain: every domain in a campaign would share one rate, which would
    # flag all of them or none of them.
    estimated_rates: bool = False

    @property
    def terminal(self) -> bool:
        """Faults a domain does not come back from, per GEX and EmailBison."""
        return self.verdict in (LISTED, MISCONFIGURED)


def _rate(numerator: float, denominator: float) -> float | None:
    return (numerator / denominator) if denominator else None


def score_mailbox(account: dict, warmup: dict | None, cfg) -> MailboxReading:
    """Judge one sending account.

    `account` is a Smartlead /email-accounts row; `warmup` the matching
    /warmup-stats payload, or None when it could not be fetched.
    """
    m = cfg.mailbox
    email = account.get("from_email") or account.get("username") or ""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    reading = MailboxReading(
        mailbox_id=int(account.get("id", 0)),
        email=email,
        domain=domain,
        verdict=UNKNOWN,
        smtp_ok=bool(account.get("is_smtp_success", True)),
        imap_ok=bool(account.get("is_imap_success", True)),
    )

    # A disconnected mailbox is not a reputation question. It cannot send at
    # all, so it is pulled regardless of every other reading.
    if not reading.smtp_ok or not reading.imap_ok:
        reading.verdict = BROKEN
        broken = [n for n, ok in (("SMTP", reading.smtp_ok), ("IMAP", reading.imap_ok)) if not ok]
        reading.reasons.append(f"{' and '.join(broken)} disconnected")
        return reading

    details = warmup or account.get("warmup_details") or {}
    sent = int(details.get("total_sent_count") or details.get("sent_count") or 0)
    spam = int(details.get("total_spam_count") or details.get("spam_count") or 0)
    reputation = details.get("warmup_reputation")
    if isinstance(reputation, str):
        reputation = reputation.rstrip("%")
    reputation = float(reputation) if reputation not in (None, "") else None

    reading.warmup_sent = sent
    reading.reputation = reputation
    reading.spam_rate = _rate(spam, sent)

    # Too little traffic to read. Reporting unknown keeps a brand-new mailbox
    # out of the bench list instead of condemning it on two data points.
    if sent < m["min_warmup_volume"]:
        reading.verdict = UNKNOWN
        reading.reasons.append(f"only {sent} warm-up sends, need {m['min_warmup_volume']}")
        return reading

    if reputation is None:
        reading.verdict = UNKNOWN
        reading.reasons.append("no warm-up reputation reported")
        return reading

    if reputation < m["reputation_bench"]:
        reading.verdict = BURNING
        reading.reasons.append(
            f"warm-up reputation {reputation:.0f}% below {m['reputation_bench']}%"
        )
    elif reputation < m["reputation_watch"]:
        reading.verdict = WATCH
        reading.reasons.append(
            f"warm-up reputation {reputation:.0f}% below {m['reputation_watch']}%"
        )
    else:
        reading.verdict = HEALTHY
    return reading


def score_domain(domain: str, zap_health: dict | None, mailboxes: list[MailboxReading],
                 stats: dict | None, cfg, estimated_rates: bool = False) -> DomainReading:
    """Judge one sending domain across both platforms.

    `stats` carries per-domain sent/reply/bounce counts derived from Smartlead;
    pass None when the domain is not currently in a campaign.
    """
    d = cfg.domain
    reading = DomainReading(
        domain=domain,
        verdict=HEALTHY,
        mailbox_ids=[m.mailbox_id for m in mailboxes],
    )

    reps = [m.reputation for m in mailboxes if m.reputation is not None]
    reading.mean_reputation = sum(reps) / len(reps) if reps else None

    reading.estimated_rates = estimated_rates
    if stats:
        sent = int(stats.get("sent", 0))
        reading.sends = sent
        reading.reply_rate = _rate(int(stats.get("replies", 0)), sent)
        reading.bounce_rate = _rate(int(stats.get("bounces", 0)), sent)

    if zap_health:
        reading.domain_id = zap_health.get("domainId")
        reading.score = zap_health.get("score")
        reading.label = zap_health.get("label")
        dns = zap_health.get("dnsRecords") or {}
        reading.spf = bool(dns.get("spfRecord", True))
        reading.dkim = bool(dns.get("dkimRecords", True))
        reading.dmarc = bool(dns.get("dmarcRecords", True))
        bl = zap_health.get("blacklistStatus") or {}
        reading.blacklisted = bool(bl.get("isBlacklisted"))
        reading.blacklists = list(bl.get("deepCheckProviders") or []) + list(
            bl.get("ipCheckProviders") or []
        )

    # Ordered by severity. The first matching fault wins so the reason line
    # names the thing that actually decides the outcome.

    missing = [
        name for name, present, required in (
            ("SPF", reading.spf, d["require_spf"]),
            ("DKIM", reading.dkim, d["require_dkim"]),
            ("DMARC", reading.dmarc, d["require_dmarc"]),
        ) if required and not present
    ]
    if missing:
        reading.verdict = MISCONFIGURED
        reading.reasons.append(f"missing {', '.join(missing)}")
        return reading

    terminal_hits = [b for b in reading.blacklists
                     if any(t.lower() in b.lower() for t in d["terminal_blacklists"])]
    if terminal_hits:
        reading.verdict = LISTED
        reading.reasons.append(f"listed on {', '.join(sorted(set(terminal_hits)))}")
        return reading

    if reading.sends >= d["min_sends_for_rates"] and not reading.estimated_rates:
        if reading.bounce_rate is not None and reading.bounce_rate > d["bounce_rate_ceiling"]:
            reading.verdict = DEGRADED
            reading.reasons.append(
                f"bounce rate {reading.bounce_rate:.2%} above {d['bounce_rate_ceiling']:.0%}"
            )
            return reading
        if reading.reply_rate is not None and reading.reply_rate < d["reply_rate_floor"]:
            reading.verdict = DEGRADED
            reading.reasons.append(
                f"reply rate {reading.reply_rate:.2%} below {d['reply_rate_floor']:.0%}"
            )
            return reading

    if (reading.mean_reputation is not None
            and reading.mean_reputation <= d["mean_reputation_swap"]):
        reading.verdict = DEGRADED
        reading.reasons.append(
            f"mean mailbox reputation {reading.mean_reputation:.0f}% at or below "
            f"{d['mean_reputation_swap']}%"
        )
        return reading

    if reading.score is not None and reading.score < d["score_floor"]:
        reading.verdict = DEGRADED
        reading.reasons.append(f"Zapmail score {reading.score} below {d['score_floor']}")
    return reading


def mark_worst_by_reply(readings: list[DomainReading], cfg) -> list[DomainReading]:
    """Demote the worst slice of the pool by reply rate.

    GEX ranks domains against each other every week, not only against a fixed
    floor: a domain looks acceptable right up until you compare it to the rest
    of the pool. Only domains with enough volume to rank are considered.
    """
    d = cfg.domain
    fraction = d["worst_fraction_by_reply"]
    eligible = [r for r in readings
                if r.reply_rate is not None and r.sends >= d["min_sends_for_rates"]
                and not r.estimated_rates]
    if len(eligible) < 5 or fraction <= 0:
        # Ranking a handful of domains produces noise, not a signal.
        return readings

    eligible.sort(key=lambda r: r.reply_rate or 0.0)
    cutoff = max(1, int(len(eligible) * fraction))
    for reading in eligible[:cutoff]:
        if reading.verdict == HEALTHY:
            reading.verdict = DEGRADED
            reading.reasons.append(
                f"reply rate {reading.reply_rate:.2%} in the worst "
                f"{fraction:.0%} of {len(eligible)} ranked domains"
            )
    return readings


def campaign_rates(analytics: dict) -> dict:
    """Normalise a Smartlead analytics payload to counts the policy can use."""
    def num(*keys: str) -> int:
        for key in keys:
            value = analytics.get(key)
            if value not in (None, ""):
                try:
                    return int(float(value))
                except (TypeError, ValueError):
                    continue
        return 0

    sent = num("sent_count", "sent", "total_sent")
    bounced = num("bounce_count", "bounced_count", "bounces")
    replied = num("reply_count", "replied_count", "replies")
    unique = num("unique_sent_count") or sent
    return {
        "sent": sent,
        "bounces": bounced,
        "replies": replied,
        "bounce_rate": _rate(bounced, sent),
        "reply_rate": _rate(replied, unique),
    }
