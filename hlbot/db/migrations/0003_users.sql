-- Dashboard accounts and their sign-in sessions (role-based access).
-- Passwords are stored only as salted scrypt hashes; a session is stored
-- only as the SHA-256 of its random token, so a copy of this database
-- does not let anyone sign in.
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_ms INTEGER NOT NULL
);

CREATE INDEX sessions_user ON sessions (user_id);
