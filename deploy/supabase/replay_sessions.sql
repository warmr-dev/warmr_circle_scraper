-- Version B (session replay experiment) — Supabase / Postgres schema.
--
-- The application creates every table automatically via SQLAlchemy's
-- create_all() the first time it connects to CIRCLE_LEADS_DB, so running this
-- by hand is OPTIONAL. It exists so you can provision the table in Supabase's
-- SQL editor explicitly, review the exact shape, and set access controls.
--
-- What it stores: one row per Circle community host, holding the AES-GCM
-- ENCRYPTED cookie blob (never plaintext) plus non-sensitive metadata and the
-- outcome of the last server-side replay attempt.

CREATE TABLE IF NOT EXISTS replay_sessions (
    id                SERIAL PRIMARY KEY,
    host              VARCHAR(255) NOT NULL,
    encrypted_cookies TEXT         NOT NULL,   -- AES-GCM ciphertext, base64
    cookie_count      INTEGER      NOT NULL DEFAULT 0,
    member_label      VARCHAR(255),
    last_result       VARCHAR(32),             -- ok | challenged | expired | error
    last_detail       TEXT,
    last_attempt_at   TIMESTAMP WITHOUT TIME ZONE,
    created_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at        TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT uq_replay_session_host UNIQUE (host)
);

CREATE INDEX IF NOT EXISTS ix_replay_sessions_host ON replay_sessions (host);

-- Security note (Supabase):
-- This table holds captured Circle sessions. The app connects with the
-- Postgres service/owner role (via CIRCLE_LEADS_DB), which bypasses RLS, so it
-- works without any policy. But you should make sure the table is NOT exposed
-- through Supabase's public REST/anon API. Either keep it out of the exposed
-- schema, or enable RLS with no anon policy so the anon/publishable key cannot
-- read it:
--
--   ALTER TABLE replay_sessions ENABLE ROW LEVEL SECURITY;
--   -- (add no policy for the anon role -> anon reads return nothing)
--
-- The encryption key (CIRCLE_CRED_KEY) is NEVER stored in the database. Keep it
-- only in the backend's environment; without it the ciphertext is useless.
