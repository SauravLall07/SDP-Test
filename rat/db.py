from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('clone', 'zip')),
    source TEXT NOT NULL,
    local_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    reference TEXT NOT NULL DEFAULT 'HEAD',
    head_sha TEXT,
    commit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    analyzed_at TEXT
);

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    UNIQUE(repository_id, name, email)
);

CREATE TABLE IF NOT EXISTS commits (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    committer_date INTEGER NOT NULL,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    UNIQUE(repository_id, sha)
);

CREATE TABLE IF NOT EXISTS changes (
    commit_id INTEGER NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'directory')),
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    PRIMARY KEY(commit_id, path, kind)
);

CREATE INDEX IF NOT EXISTS idx_commits_repo_date
    ON commits(repository_id, committer_date);
CREATE INDEX IF NOT EXISTS idx_commits_author ON commits(author_id);
CREATE INDEX IF NOT EXISTS idx_changes_path ON changes(path, kind);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.executescript(SCHEMA)
