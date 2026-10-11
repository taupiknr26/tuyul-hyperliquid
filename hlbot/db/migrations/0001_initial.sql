-- Schema v1 (PRD §12.1). Forward-only; later migrations must keep the
-- previous release working for one version (NFR-019).

CREATE TABLE kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE config_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    hash           TEXT NOT NULL UNIQUE,
    effective_from TEXT NOT NULL,
    settings       TEXT NOT NULL
);

CREATE TABLE account_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    env           TEXT NOT NULL,
    dex           TEXT NOT NULL,
    account_value REAL,
    margin_used   REAL,
    total_ntl     REAL,
    withdrawable  REAL,
    snapshot_at   TEXT NOT NULL
);

CREATE TABLE messages (
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    channel_ref TEXT,
    sent_at     TEXT,
    received_at TEXT NOT NULL,
    text        TEXT NOT NULL,
    has_media   INTEGER NOT NULL DEFAULT 0,
    reply_to    INTEGER,
    edited_at   TEXT,
    deleted_at  TEXT,
    PRIMARY KEY (channel_id, message_id)
);

CREATE TABLE signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        INTEGER NOT NULL,
    message_id        INTEGER NOT NULL,
    channel_ref       TEXT,
    content_id        TEXT,
    type              TEXT NOT NULL,
    symbol            TEXT,
    side              TEXT,
    zone_low          REAL,
    zone_high         REAL,
    sl                REAL,
    tps               TEXT,
    parse_errors      TEXT NOT NULL DEFAULT '[]',
    valid             INTEGER NOT NULL DEFAULT 0,
    validation_errors TEXT NOT NULL DEFAULT '[]',
    parser_version    TEXT,
    state             TEXT NOT NULL,
    reason            TEXT,
    signal_time       TEXT,
    expires_at        TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (channel_id, message_id),
    FOREIGN KEY (channel_id, message_id) REFERENCES messages (channel_id, message_id)
);
CREATE INDEX idx_signals_state ON signals (state);
CREATE INDEX idx_signals_content ON signals (content_id);

CREATE TABLE plans (
    signal_id      INTEGER PRIMARY KEY REFERENCES signals (id),
    coin           TEXT NOT NULL,
    asset_id       INTEGER,
    effective_low  REAL NOT NULL,
    effective_high REAL NOT NULL,
    buffer         REAL NOT NULL,
    sl             REAL NOT NULL,
    tp_levels      TEXT NOT NULL,
    config_version INTEGER REFERENCES config_versions (id)
);

CREATE TABLE risk_assessments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id  INTEGER NOT NULL REFERENCES signals (id),
    attempt    INTEGER NOT NULL,
    at         TEXT NOT NULL,
    inputs     TEXT NOT NULL,
    outputs    TEXT NOT NULL,
    approved   INTEGER NOT NULL,
    violations TEXT NOT NULL
);
CREATE INDEX idx_risk_signal ON risk_assessments (signal_id);

CREATE TABLE trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id          INTEGER NOT NULL REFERENCES signals (id),
    risk_assessment_id INTEGER REFERENCES risk_assessments (id),
    attempt            INTEGER NOT NULL,
    env                TEXT NOT NULL,
    coin               TEXT NOT NULL,
    side               TEXT NOT NULL,
    state              TEXT NOT NULL,
    planned_size       REAL NOT NULL,
    filled_size        REAL NOT NULL DEFAULT 0,
    avg_entry          REAL,
    remaining_size     REAL NOT NULL DEFAULT 0,
    limit_price        REAL,
    sl_price           REAL NOT NULL,
    tp_plan            TEXT NOT NULL,
    opened_at          TEXT,
    protected_at       TEXT,
    closed_at          TEXT,
    close_reason       TEXT,
    outcome            TEXT,
    realized_pnl       REAL,
    fees               REAL,
    funding            REAL,
    net_pnl            REAL,
    config_version     INTEGER REFERENCES config_versions (id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (signal_id, attempt)
);
CREATE INDEX idx_trades_state ON trades (state);
CREATE INDEX idx_trades_coin ON trades (coin);

CREATE TABLE orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades (id),
    role            TEXT NOT NULL CHECK (role IN ('ENTRY', 'SL', 'TP', 'CLOSE')),
    leg             INTEGER NOT NULL DEFAULT 0,
    attempt         INTEGER NOT NULL DEFAULT 1,
    cloid           TEXT NOT NULL UNIQUE,
    oid             INTEGER,
    coin            TEXT NOT NULL,
    side            TEXT NOT NULL,
    type            TEXT NOT NULL,
    tif             TEXT,
    reduce_only     INTEGER NOT NULL,
    price           REAL NOT NULL,
    trigger_price   REAL,
    size            REAL NOT NULL,
    filled_size     REAL NOT NULL DEFAULT 0,
    avg_fill_price  REAL,
    nonce           INTEGER,
    expires_after   INTEGER,
    state           TEXT NOT NULL,
    exchange_status TEXT,
    error_text      TEXT,
    created_at      TEXT NOT NULL,
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_orders_trade ON orders (trade_id);
CREATE INDEX idx_orders_state ON orders (state);
CREATE INDEX idx_orders_oid ON orders (oid);

CREATE TABLE fills (
    tid            INTEGER PRIMARY KEY,
    order_id       INTEGER REFERENCES orders (id),
    trade_id       INTEGER REFERENCES trades (id),
    role           TEXT,
    oid            INTEGER,
    cloid          TEXT,
    coin           TEXT NOT NULL,
    side           TEXT NOT NULL,
    price          REAL NOT NULL,
    size           REAL NOT NULL,
    fee            REAL NOT NULL DEFAULT 0,
    fee_token      TEXT,
    closed_pnl     REAL NOT NULL DEFAULT 0,
    crossed        INTEGER,
    dir            TEXT,
    start_position REAL,
    time           INTEGER NOT NULL,
    hash           TEXT,
    liquidation    INTEGER NOT NULL DEFAULT 0,
    recorded_at    TEXT NOT NULL
);
CREATE INDEX idx_fills_trade ON fills (trade_id);
CREATE INDEX idx_fills_time ON fills (time);

CREATE TABLE position_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    coin           TEXT NOT NULL,
    szi            REAL NOT NULL,
    entry_px       REAL,
    unrealized_pnl REAL,
    leverage       REAL,
    liquidation_px REAL,
    margin_used    REAL,
    cum_funding    REAL,
    at             TEXT NOT NULL,
    source         TEXT NOT NULL
);
CREATE INDEX idx_possnap_coin_at ON position_snapshots (coin, at);

CREATE TABLE funding_payments (
    time     INTEGER NOT NULL,
    coin     TEXT NOT NULL,
    usdc     REAL NOT NULL,
    rate     REAL,
    szi      REAL,
    trade_id INTEGER REFERENCES trades (id),
    PRIMARY KEY (time, coin)
);

CREATE TABLE day_state (
    utc_date          TEXT PRIMARY KEY,
    start_equity      REAL NOT NULL,
    loss_limit_hit_at TEXT,
    target_hit_at     TEXT,
    target_notified   INTEGER NOT NULL DEFAULT 0
);

-- Operator intent (pause / resume). Exactly one row.
CREATE TABLE control_state (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    mode   TEXT NOT NULL CHECK (mode IN ('RUNNING', 'PAUSED', 'HALTED')),
    reason TEXT,
    set_by TEXT,
    set_at TEXT NOT NULL
);

-- Automatic pause conditions (§10.7). scope is 'global' or a coin.
CREATE TABLE conditions (
    key         TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('block', 'pause', 'halt')),
    reason      TEXT NOT NULL,
    auto_resume INTEGER NOT NULL,
    set_at      TEXT NOT NULL
);

-- Append-only audit log (FR-090, NFR-009).
CREATE TABLE audit_events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    kind           TEXT NOT NULL,
    correlation_id TEXT,
    signal_id      INTEGER,
    trade_id       INTEGER,
    order_id       INTEGER,
    payload        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_audit_signal ON audit_events (signal_id);
CREATE INDEX idx_audit_trade ON audit_events (trade_id);
CREATE INDEX idx_audit_order ON audit_events (order_id);
CREATE INDEX idx_audit_kind ON audit_events (kind);

CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

-- Notification outbox (FR-092).
CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    critical        INTEGER NOT NULL DEFAULT 0,
    text            TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    sent_at         TEXT,
    dropped_at      TEXT,
    last_error      TEXT
);
CREATE INDEX idx_outbox_pending ON outbox (sent_at, dropped_at, id);
