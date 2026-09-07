#!/usr/bin/env python3
"""Deliverability engine: monitor sending infrastructure, bench what is burnt,
rotate in what is warm.

  python3 run.py --check          verify credentials and API paths, change nothing
  python3 run.py                  plan only: print what it would do and why
  python3 run.py --apply          execute the plan
  python3 run.py --offline        run the full decision path against fixtures

Planning is the default. Nothing mutates without --apply, and the API clients
themselves refuse writes unless it is set.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

import requests

from engine import health as H
from engine.actions import Executor
from engine.config import Config
from engine.policy import BENCH, ESCALATE, PAUSE_CAMPAIGN, RAMP, REPLACE, RETIRE, decide
from engine.smartlead import SmartleadClient
from engine.store import open_store
from engine.zapmail import ZapmailClient

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("engine")


# ---------------------------------------------------------------- gathering

class Fixtures:
    """Stand-in for both APIs so the decision path runs with no network.

    The offline path exercises the same scoring and policy code as a live run;
    only the transport differs.
    """

    def __init__(self, directory: Path):
        self.dir = directory

    def _load(self, name: str):
        return json.loads((self.dir / f"{name}.json").read_text())

    def campaigns(self):
        return self._load("campaigns")

    def active_campaigns(self):
        return [c for c in self.campaigns() if str(c.get("status", "")).upper() == "ACTIVE"]

    def campaign_analytics(self, campaign_id):
        return self._load("analytics").get(str(campaign_id), {})

    def email_accounts(self):
        return self._load("email_accounts")

    def campaign_email_accounts(self, campaign_id):
        return [a for a in self.email_accounts()
                if campaign_id in a.get("campaign_ids", [])]

    def warmup_stats(self, account_id):
        return self._load("warmup").get(str(account_id), {})

    def domain_stats(self):
        return self._load("domain_stats")

    def domains_with_mailboxes(self):
        return self._load("zapmail_domains")

    def domain_health(self, domain_id):
        return self._load("zapmail_health").get(domain_id, {})

    def prewarmed_domains(self):
        return self._load("zapmail_prewarmed")

    def verify(self):
        return {"workspaces": "ok (offline)", "domains": "ok (offline)",
                "mailboxes": "ok (offline)"}


def gather_domain_stats(sl, campaign_id, accounts) -> tuple[dict, bool]:
    """Per-domain sent/reply/bounce, and whether the numbers are measured.

    Smartlead's per-account statistics route is not in the published index, so
    it is attempted and its absence handled. The fallback apportions campaign
    totals by each domain's share of daily sending volume, which is fine for
    reporting and useless for judging: every domain would carry the same rate,
    so the policy declines to act on estimated numbers. Reputation, DNS and
    blacklist signals are unaffected and still decide.
    """
    by_domain: dict[str, dict] = defaultdict(lambda: {"sent": 0, "replies": 0, "bounces": 0})

    try:
        rows = sl.campaign_mailbox_stats(campaign_id)
    except Exception as exc:
        log.info("per-mailbox statistics unavailable (%s); estimating per domain", exc)
        rows = []

    if rows:
        for row in rows:
            email = (row.get("from_email") or row.get("email") or "").lower()
            if "@" not in email:
                continue
            bucket = by_domain[email.split("@")[-1]]
            bucket["sent"] += int(row.get("sent_count") or row.get("sent") or 0)
            bucket["replies"] += int(row.get("reply_count") or row.get("replies") or 0)
            bucket["bounces"] += int(row.get("bounce_count") or row.get("bounces") or 0)
        if any(b["sent"] for b in by_domain.values()):
            return dict(by_domain), False

    rates = H.campaign_rates(sl.campaign_analytics(campaign_id))
    weights: dict[str, int] = defaultdict(int)
    for account in accounts:
        email = (account.get("from_email") or "").lower()
        if "@" in email:
            weights[email.split("@")[-1]] += max(1, int(account.get("message_per_day") or 1))

    total = sum(weights.values()) or 1
    for domain, weight in weights.items():
        share = weight / total
        by_domain[domain] = {
            "sent": int(rates["sent"] * share),
            "replies": int(rates["replies"] * share),
            "bounces": int(rates["bounces"] * share),
        }
    return dict(by_domain), True


# ---------------------------------------------------------------- reporting

def print_plan(campaign, plan, mailbox_readings, domain_readings) -> None:
    name = campaign.get("name", campaign["id"])
    rates = campaign["rates"]
    print(f"\n{'=' * 78}\nCAMPAIGN  {name}  (id {campaign['id']})")
    reply = f"{rates['reply_rate']:.2%}" if rates.get("reply_rate") is not None else "n/a"
    bounce = f"{rates['bounce_rate']:.2%}" if rates.get("bounce_rate") is not None else "n/a"
    print(f"  {rates['sent']} sent · reply {reply} · bounce {bounce}")

    counts = defaultdict(int)
    for reading in mailbox_readings:
        counts[reading.verdict] += 1
    print("  mailboxes: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))

    if any(d.estimated_rates for d in domain_readings.values()):
        print("  note: per-domain reply/bounce estimated from campaign totals; "
              "not used to condemn a domain")

    flagged = [d for d in domain_readings.values() if d.verdict != "healthy"]
    if flagged:
        print("\n  DOMAINS FLAGGED")
        for reading in sorted(flagged, key=lambda d: d.domain):
            print(f"    {reading.domain:<34} {reading.verdict:<14} {'; '.join(reading.reasons)}")

    if not plan.actions:
        print("\n  No action. Everything within policy.")
        return

    print("\n  PLAN")
    for action in plan.actions:
        print(f"    {action.describe()}")

    summary = defaultdict(int)
    for action in plan.actions:
        summary[action.action] += 1
    print("\n  " + " · ".join(f"{v} {k}" for k, v in sorted(summary.items())))


def slack_digest(webhook: str, lines: list[str]) -> None:
    try:
        requests.post(webhook, json={"text": "\n".join(lines)}, timeout=15)
    except Exception as exc:  # a failed notification must not fail the run
        log.warning("slack digest failed: %s", exc)


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="execute the plan (default is plan only)")
    parser.add_argument("--check", action="store_true",
                        help="verify credentials and API paths, then exit")
    parser.add_argument("--offline", action="store_true",
                        help="run against tests/fixtures with no network")
    parser.add_argument("--campaign", type=int, action="append",
                        help="limit to one campaign id (repeatable)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--fixtures", default=str(ROOT / "tests" / "fixtures"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(name)s  %(message)s",
    )

    if args.apply and args.offline:
        parser.error("--apply cannot be combined with --offline: "
                     "fixtures have nothing to apply to")

    cfg = Config.load(args.config)
    read_only = not args.apply

    if args.offline:
        source = Fixtures(Path(args.fixtures))
        sl = zm = source
    else:
        cfg.require("smartlead_api_key", "zapmail_api_key")
        sl = SmartleadClient(cfg.secrets.smartlead_api_key,
                             cfg.smartlead["base_url"], read_only=read_only)
        zm = ZapmailClient(cfg.secrets.zapmail_api_key, cfg.zapmail["base_url"],
                           cfg.zapmail["paths"], cfg.secrets.zapmail_workspace_key,
                           read_only=read_only)

    if args.check:
        print("Zapmail path check:")
        for name, status in zm.verify().items():
            print(f"  {name:<12} {status}")
        print("\nSmartlead:")
        try:
            campaigns = sl.campaigns()
            print(f"  campaigns    ok ({len(campaigns)} returned)")
        except Exception as exc:
            print(f"  campaigns    {exc}")
        store = open_store(cfg)
        backend = "postgres" if cfg.secrets.database_url else f"sqlite ({cfg.secrets.sqlite_path})"
        print(f"\nStore: {backend}")
        store.close()
        return 0

    store = open_store(cfg)
    run_id = store.start_run("apply" if args.apply else "plan")
    mode = "APPLY" if args.apply else "PLAN (nothing will change)"
    print(f"run {run_id} · {mode}" + ("  · offline fixtures" if args.offline else ""))

    # Infrastructure inventory, read once and shared across campaigns.
    zap_domains = zm.domains_with_mailboxes()
    zap_health: dict[str, dict] = {}
    for entry in zap_domains:
        domain_id = entry.get("id")
        name = (entry.get("domain") or "").lower()
        if not domain_id or not name:
            continue
        try:
            payload = zm.domain_health(domain_id)
            payload["domainId"] = domain_id
            zap_health[name] = payload
        except Exception as exc:
            log.warning("health lookup failed for %s: %s", name, exc)

    reserve = zm.prewarmed_domains()
    reserve_count = sum(len(d.get("mailboxes", []) or []) for d in reserve) or len(reserve)

    states = store.all_states()
    campaigns = sl.active_campaigns()
    if args.campaign:
        campaigns = [c for c in campaigns if int(c["id"]) in set(args.campaign)]
    elif cfg.smartlead["campaigns"]:
        wanted = {int(c) for c in cfg.smartlead["campaigns"]}
        campaigns = [c for c in campaigns if int(c["id"]) in wanted]

    if not campaigns:
        print("No active campaigns in scope.")
        store.finish_run(run_id, "no campaigns in scope")
        return 0

    digest: list[str] = [f"*Deliverability run* `{run_id}` — {mode}"]
    totals = {"applied": 0, "planned": 0, "failed": 0}

    for campaign in campaigns:
        campaign_id = int(campaign["id"])
        accounts = sl.campaign_email_accounts(campaign_id)
        if not accounts:
            continue

        mailbox_readings = []
        for account in accounts:
            try:
                warmup = sl.warmup_stats(int(account["id"]))
            except Exception:
                warmup = None
            reading = H.score_mailbox(account, warmup, cfg)
            reading.campaign_id = campaign_id
            mailbox_readings.append(reading)

        by_domain: dict[str, list] = defaultdict(list)
        for reading in mailbox_readings:
            by_domain[reading.domain].append(reading)

        if args.offline:
            stats, estimated = source.domain_stats(), False
        else:
            stats, estimated = gather_domain_stats(sl, campaign_id, accounts)

        domain_readings = {
            name: H.score_domain(name, zap_health.get(name), readings,
                                 stats.get(name), cfg, estimated_rates=estimated)
            for name, readings in by_domain.items()
        }
        H.mark_worst_by_reply(list(domain_readings.values()), cfg)

        analytics = sl.campaign_analytics(campaign_id)
        campaign_ctx = {
            "id": campaign_id,
            "name": campaign.get("name", str(campaign_id)),
            "rates": H.campaign_rates(analytics),
        }

        plan = decide(campaign_ctx, mailbox_readings, domain_readings,
                      states, reserve_count, cfg)
        print_plan(campaign_ctx, plan, mailbox_readings, domain_readings)

        store.record_mailbox_health(run_id, [r.__dict__ for r in mailbox_readings])
        store.record_domain_health(run_id, [r.__dict__ for r in domain_readings.values()])

        counts = Executor(sl, zm, store, cfg, run_id, args.apply).run(plan)
        for key in totals:
            totals[key] += counts[key]

        if plan.actions:
            digest.append(f"\n*{campaign_ctx['name']}*")
            for action in plan.actions[:12]:
                digest.append(f"• {action.describe()}")

    summary = (f"{totals['applied']} applied, {totals['planned']} planned, "
               f"{totals['failed']} failed")
    print(f"\n{'=' * 78}\n{summary}")
    if not args.apply and totals["planned"]:
        print("Nothing changed. Re-run with --apply to execute.")

    store.finish_run(run_id, summary)
    store.close()

    if cfg.secrets.slack_webhook_url and len(digest) > 1:
        digest.append(f"\n_{summary}_")
        slack_digest(cfg.secrets.slack_webhook_url, digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
