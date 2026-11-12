-- PulseWatch schema (SQLite)
-- Timestamps are stored as UTC ISO-8601 strings: YYYY-MM-DDTHH:MM:SSZ

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS websites (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    url            TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    current_status TEXT    NOT NULL DEFAULT 'UNKNOWN'
                           CHECK (current_status IN ('UP', 'DOWN', 'DEGRADED', 'UNKNOWN')),
    last_checked   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_websites_url
    ON websites (url COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS monitoring_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    website_id       INTEGER NOT NULL
                     REFERENCES websites (id) ON DELETE CASCADE,
    status           TEXT    NOT NULL
                     CHECK (status IN ('UP', 'DOWN', 'DEGRADED')),
    http_status_code INTEGER,
    response_time_ms REAL,
    error_message    TEXT,
    checked_at       TEXT    NOT NULL,
    resolved_ip      TEXT
);

CREATE INDEX IF NOT EXISTS idx_results_website_checked
    ON monitoring_results (website_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_results_checked
    ON monitoring_results (checked_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    website_id       INTEGER NOT NULL
                     REFERENCES websites (id) ON DELETE CASCADE,
    incident_status  TEXT    NOT NULL DEFAULT 'OPEN'
                     CHECK (incident_status IN ('OPEN', 'RESOLVED')),
    started_at       TEXT    NOT NULL,
    resolved_at      TEXT,
    duration_seconds INTEGER,
    description      TEXT
);

CREATE INDEX IF NOT EXISTS idx_incidents_website_started
    ON incidents (website_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_status
    ON incidents (incident_status, started_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    monitoring_interval INTEGER NOT NULL,
    timeout_seconds     INTEGER NOT NULL,
    fast_threshold      INTEGER NOT NULL,
    moderate_threshold  INTEGER NOT NULL
);
