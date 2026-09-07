-- State store for the deliverability engine.
-- Runs against Supabase/Postgres as written; store.py rewrites the few
-- Postgres-only types when it falls back to SQLite.

-- One row per engine execution. Everything else hangs off run_id, so any
-- decision can be traced back to the readings that produced it.
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    mode         TEXT NOT NULL,          -- 'plan' or 'apply'
    summary      TEXT
);

-- Per-mailbox reading. Kept every run so a reputation slide is visible as a
-- trend rather than a single alarming number.
CREATE TABLE IF NOT EXISTS mailbox_health (
    run_id       TEXT NOT NULL REFERENCES runs(id),
    observed_at  TIMESTAMPTZ NOT NULL,
    mailbox_id   BIGINT NOT NULL,
    email        TEXT NOT NULL,
    domain       TEXT NOT NULL,
    campaign_id  BIGINT,
    reputation   REAL,
    spam_rate    REAL,
    warmup_sent  INTEGER,
    smtp_ok      BOOLEAN,
    imap_ok      BOOLEAN,
    verdict      TEXT NOT NULL,          -- healthy | watch | burning | broken | unknown
    reasons      TEXT,
    PRIMARY KEY (run_id, mailbox_id)
);

-- Per-domain reading from Zapmail. Domain reputation is shared by every
-- mailbox on it, which is why it is scored separately and can condemn a whole
-- group at once.
CREATE TABLE IF NOT EXISTS domain_health (
    run_id       TEXT NOT NULL REFERENCES runs(id),
    observed_at  TIMESTAMPTZ NOT NULL,
    domain       TEXT NOT NULL,
    workspace    TEXT,
    score        INTEGER,
    label        TEXT,
    blacklisted  BOOLEAN,
    blacklists   TEXT,
    spf          BOOLEAN,
    dkim         BOOLEAN,
    dmarc        BOOLEAN,
    verdict      TEXT NOT NULL,          -- healthy | degraded | misconfigured | blacklisted
    reasons      TEXT,
    PRIMARY KEY (run_id, domain)
);

-- Every action the engine decided on, whether or not it was executed.
-- Planned-but-not-applied rows are the audit trail for dry runs.
CREATE TABLE IF NOT EXISTS actions (
    id           BIGSERIAL PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    created_at   TIMESTAMPTZ NOT NULL,
    action       TEXT NOT NULL,          -- bench | throttle | ramp | replace | pause_campaign | escalate
    campaign_id  BIGINT,
    mailbox_id   BIGINT,
    target       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    payload      TEXT,
    applied      BOOLEAN NOT NULL DEFAULT FALSE,
    error        TEXT
);

-- Current disposition of each mailbox. This is what enforces cooldowns and
-- makes the engine idempotent across runs.
CREATE TABLE IF NOT EXISTS mailbox_state (
    mailbox_id     BIGINT PRIMARY KEY,
    email          TEXT NOT NULL,
    domain         TEXT NOT NULL,
    status         TEXT NOT NULL,        -- active | benched | retired
    daily_cap      INTEGER,
    benched_at     TIMESTAMPTZ,
    cooldown_until TIMESTAMPTZ,
    bench_count    INTEGER NOT NULL DEFAULT 0,
    last_seen_at   TIMESTAMPTZ,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_mailbox_health_mailbox ON mailbox_health (mailbox_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_domain_health_domain   ON domain_health (domain, observed_at);
CREATE INDEX IF NOT EXISTS idx_actions_run            ON actions (run_id);
