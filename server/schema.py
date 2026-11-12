"""SQLite 테이블 스키마 정의."""

INIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    reserved_at TEXT NOT NULL,
    status TEXT NOT NULL,
    allow_during_off_period INTEGER NOT NULL DEFAULT 0,
    cleaning_type TEXT NOT NULL DEFAULT 'mopAfterVacuum',
    failure_reason TEXT,
    last_device_state TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS off_periods (
    id TEXT PRIMARY KEY,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_stop_events (
    id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    off_period_id TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    details TEXT
);

CREATE TABLE IF NOT EXISTS water_trackers (
    id TEXT PRIMARY KEY,
    last_refilled_at TEXT,
    mop_clean_count INTEGER NOT NULL DEFAULT 0,
    alert_threshold_n INTEGER NOT NULL DEFAULT 7,
    last_alerted_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_targets (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL UNIQUE,
    push_token TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dnd_backup (
    id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
