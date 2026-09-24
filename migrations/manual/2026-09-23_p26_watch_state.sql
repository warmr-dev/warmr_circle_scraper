-- p26: where the fast poller has got to in each community's feed.
--
-- The harvest walks every space and takes hours, so a post published just
-- after it passed waits for the next run: measured on 2026-09-23, a fresh post
-- was stored 31 hours after publication at the median. This table lets a
-- separate service ask each community's newest-first feed for one page every
-- couple of minutes instead.
--
-- The watermark is an id, not a timestamp. A timestamp loses posts to clock
-- skew, to a post edited after publication, and to a read that failed and was
-- recorded as "nothing new" -- all three happened with the old reader.

CREATE TABLE IF NOT EXISTS watch_state (
    id                  SERIAL PRIMARY KEY,
    community_id        INTEGER NOT NULL UNIQUE REFERENCES communities (id),
    host                VARCHAR(255) NOT NULL,

    -- anon | cookie | off
    mode                VARCHAR(16) NOT NULL DEFAULT 'anon',
    -- fast | slow: a community silent for weeks is polled slowly until it
    -- posts again, which is what keeps the list inside one IP's budget.
    tier                VARCHAR(16) NOT NULL DEFAULT 'fast',

    last_post_id        BIGINT,
    -- The last few hundred ids seen. Circle can publish an id below the
    -- newest one when a draft is released, so a watermark alone is not enough.
    recent_ids          JSONB NOT NULL DEFAULT '[]'::jsonb,
    etag                VARCHAR(255),

    next_check_at       TIMESTAMP NOT NULL DEFAULT now(),
    last_checked_at     TIMESTAMP,
    last_new_at         TIMESTAMP,

    last_status         VARCHAR(32),
    last_detail         TEXT,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0,

    posts_seen          INTEGER NOT NULL DEFAULT 0,
    leads_found         INTEGER NOT NULL DEFAULT 0,

    created_at          TIMESTAMP NOT NULL DEFAULT now(),
    updated_at          TIMESTAMP NOT NULL DEFAULT now()
);

-- The poller's only hot query: "what is due?"
CREATE INDEX IF NOT EXISTS ix_watch_state_due ON watch_state (next_check_at);
CREATE INDEX IF NOT EXISTS ix_watch_state_host ON watch_state (host);
CREATE INDEX IF NOT EXISTS ix_watch_state_mode ON watch_state (mode);
CREATE INDEX IF NOT EXISTS ix_watch_state_tier ON watch_state (tier);
CREATE INDEX IF NOT EXISTS ix_watch_state_status ON watch_state (last_status);
