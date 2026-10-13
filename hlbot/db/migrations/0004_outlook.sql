-- Scenario outlook (docs/outlook.md): the generated briefs, and each
-- user's own layer on top of them. Nothing here is read by the trading
-- engine. A brief is stored whole as JSON, exactly as it was published,
-- so the review of past briefs shows what was said at the time.
CREATE TABLE outlooks (
    id             TEXT PRIMARY KEY,           -- 2026-10-12-newyork, or ...-2 for a refresh within the session
    slot_id        TEXT NOT NULL,              -- the scheduled brief it belongs to
    instrument     TEXT NOT NULL,
    session        TEXT NOT NULL CHECK (session IN ('asia', 'london', 'newyork')),
    trigger        TEXT NOT NULL,              -- scheduled | catchup | event | manual
    generated_at   TEXT NOT NULL,
    generated_ms   INTEGER NOT NULL,
    valid_until    TEXT NOT NULL,
    valid_until_ms INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,           -- outcomes are read up to here: valid_until, or earlier when a refresh replaced it
    stale_at       TEXT,                       -- set when it stopped being the view before valid_until
    stale_kind     TEXT,                       -- event | levels | replaced
    stale_reason   TEXT,
    body           TEXT NOT NULL,              -- the brief
    states         TEXT NOT NULL DEFAULT '{}', -- per scenario: state, outcome, when
    final          INTEGER NOT NULL DEFAULT 0, -- outcomes are settled
    tracked_ms     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX outlooks_generated ON outlooks (generated_ms);
CREATE INDEX outlooks_open ON outlooks (final);

-- "My Version": a user's scenario edits and journal note for one brief.
-- The generated brief is never changed by them.
CREATE TABLE outlook_user_layers (
    username     TEXT NOT NULL,
    outlook_id   TEXT NOT NULL,
    edits        TEXT NOT NULL DEFAULT '{}',   -- per scenario key: the edited fields
    states       TEXT NOT NULL DEFAULT '{}',   -- outcomes of the edited scenarios, tracked apart from the generated ones
    journal_note TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (username, outlook_id)
);

-- A user's own levels. They carry over from brief to brief while they
-- stay near price; one that is left far behind is archived, not deleted.
CREATE TABLE outlook_user_levels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    price       REAL NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('support', 'resistance')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    archived_at TEXT,
    archived_by TEXT                           -- the brief whose price left it behind
);

CREATE INDEX outlook_user_levels_user ON outlook_user_levels (username, archived_at);
