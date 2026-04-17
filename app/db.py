"""SQLite database for integrations and lead events.

Replaces JSON file storage — atomic transactions, no race conditions.
Uses data_dir from config (set to /data on Render with persistent disk).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_DB_PATH: Path | None = None
_CONNECTION: sqlite3.Connection | None = None


def _get_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = data_dir / "medidesk.db"
    return _DB_PATH


def get_connection() -> sqlite3.Connection:
    """Get or create a SQLite connection with WAL mode for better concurrency."""
    global _CONNECTION
    if _CONNECTION is None:
        db_path = _get_db_path()
        _CONNECTION = sqlite3.connect(str(db_path), check_same_thread=False)
        _CONNECTION.row_factory = sqlite3.Row
        _CONNECTION.execute("PRAGMA journal_mode=WAL")
        _CONNECTION.execute("PRAGMA busy_timeout=5000")
        _init_tables(_CONNECTION)
        logger.info("SQLite database initialized at %s", db_path)
    return _CONNECTION


def _safe_add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    """Add a column to an existing table if it doesn't exist (safe migration)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
        logger.info("Added column %s to %s", column, table)
    except sqlite3.OperationalError:
        pass  # Column already exists


def _init_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS integrations (
            id TEXT PRIMARY KEY,
            fb_page_id TEXT NOT NULL,
            fb_page_name TEXT NOT NULL,
            fb_page_token TEXT NOT NULL,
            fb_form_id TEXT NOT NULL,
            fb_form_name TEXT NOT NULL,
            fb_form_questions TEXT DEFAULT '[]',
            medidesk_form_id TEXT NOT NULL,
            medidesk_form_name TEXT DEFAULT '',
            medidesk_fields TEXT DEFAULT '[]',
            field_mappings TEXT DEFAULT '[]',
            active INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            facility_id TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            integration_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            mapped_fields_count INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            fb_raw_data TEXT DEFAULT '{}',
            mapped_values TEXT DEFAULT '{}',
            medidesk_form_id TEXT DEFAULT '',
            retried INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            user_data TEXT NOT NULL DEFAULT '{}',
            pages_data TEXT NOT NULL DEFAULT '[]',
            role TEXT NOT NULL DEFAULT 'user',
            facility_id TEXT DEFAULT '',
            facility_name TEXT DEFAULT '',
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS facilities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            fb_user_id TEXT NOT NULL UNIQUE,
            fb_user_name TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lead_events_integration
            ON lead_events(integration_id);
        CREATE INDEX IF NOT EXISTS idx_lead_events_status
            ON lead_events(status);
        CREATE INDEX IF NOT EXISTS idx_lead_events_lead_id
            ON lead_events(lead_id);
        CREATE INDEX IF NOT EXISTS idx_lead_events_timestamp
            ON lead_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_integrations_fb_page
            ON integrations(fb_page_id);
        CREATE INDEX IF NOT EXISTS idx_integrations_fb_form
            ON integrations(fb_page_id, fb_form_id);
        CREATE INDEX IF NOT EXISTS idx_facilities_fb_user
            ON facilities(fb_user_id);

        CREATE TABLE IF NOT EXISTS pending_registrations (
            fb_user_id TEXT PRIMARY KEY,
            fb_user_name TEXT DEFAULT '',
            attempted_at TEXT NOT NULL
        );
    """)

    # Migration: add columns to existing tables if missing
    _safe_add_column(conn, "integrations", "facility_id", "TEXT DEFAULT ''")
    _safe_add_column(conn, "sessions", "facility_id", "TEXT DEFAULT ''")
    _safe_add_column(conn, "sessions", "facility_name", "TEXT DEFAULT ''")

    # Create indexes on migrated columns (safe — column now exists)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_integrations_facility ON integrations(facility_id)")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def migrate_from_json() -> None:
    """One-time migration: import existing JSON data into SQLite."""
    conn = get_connection()

    # Migrate integrations.json
    int_path = Path(settings.data_dir) / settings.integrations_file
    if not int_path.exists():
        int_path = Path(settings.integrations_file)
    if int_path.exists():
        try:
            data = json.loads(int_path.read_text(encoding="utf-8"))
            for d in data:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO integrations
                           (id, fb_page_id, fb_page_name, fb_page_token,
                            fb_form_id, fb_form_name, fb_form_questions,
                            medidesk_form_id, medidesk_form_name, medidesk_fields,
                            field_mappings, active, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            d["id"], d["fb_page_id"], d["fb_page_name"],
                            d["fb_page_token"], d["fb_form_id"], d["fb_form_name"],
                            json.dumps(d.get("fb_form_questions", [])),
                            d["medidesk_form_id"],
                            d.get("medidesk_form_name", ""),
                            json.dumps(d.get("medidesk_fields", [])),
                            json.dumps(d.get("field_mappings", [])),
                            1 if d.get("active") else 0,
                            d.get("created_at", ""),
                            d.get("updated_at", ""),
                        ),
                    )
                except Exception:
                    logger.error("Failed to migrate integration %s", d.get("id"), exc_info=True)
            conn.commit()
            logger.info("Migrated %d integrations from JSON", len(data))
        except Exception:
            logger.error("Failed to read integrations JSON for migration", exc_info=True)

    # Migrate lead_log.json
    lead_path = Path(settings.data_dir) / settings.lead_log_file
    if not lead_path.exists():
        lead_path = Path(settings.lead_log_file)
    if lead_path.exists():
        try:
            data = json.loads(lead_path.read_text(encoding="utf-8"))
            for e in data:
                try:
                    conn.execute(
                        """INSERT INTO lead_events
                           (integration_id, lead_id, timestamp, status,
                            mapped_fields_count, error, fb_raw_data,
                            mapped_values, medidesk_form_id, retried)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            e["integration_id"], e["lead_id"],
                            e["timestamp"], e["status"],
                            e.get("mapped_fields_count", 0),
                            e.get("error", ""),
                            json.dumps(e.get("fb_raw_data", {})),
                            json.dumps(e.get("mapped_values", {})),
                            e.get("medidesk_form_id", ""),
                            1 if e.get("retried") else 0,
                        ),
                    )
                except Exception:
                    logger.error("Failed to migrate lead event", exc_info=True)
            conn.commit()
            logger.info("Migrated %d lead events from JSON", len(data))
        except Exception:
            logger.error("Failed to read lead log JSON for migration", exc_info=True)
