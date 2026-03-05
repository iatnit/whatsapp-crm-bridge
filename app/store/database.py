"""SQLite database initialization and connection management."""

import logging
from contextlib import asynccontextmanager

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_message_id TEXT UNIQUE,
    phone       TEXT    NOT NULL,
    display_name TEXT   DEFAULT '',
    direction   TEXT    NOT NULL,       -- 'inbound' / 'outbound'
    msg_type    TEXT    DEFAULT 'text', -- text/image/document/audio/video
    content     TEXT    DEFAULT '',
    media_path  TEXT    DEFAULT '',
    timestamp   INTEGER NOT NULL,
    processed   BOOLEAN DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversations (
    phone           TEXT PRIMARY KEY,
    display_name    TEXT DEFAULT '',
    customer_id     TEXT DEFAULT '',
    customer_name   TEXT DEFAULT '',
    match_status    TEXT DEFAULT 'unmatched', -- matched/unmatched/manual
    first_message_at DATETIME,
    last_message_at DATETIME,
    total_messages  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_processed ON messages(processed);
CREATE INDEX IF NOT EXISTS idx_messages_phone_processed ON messages(phone, processed);
CREATE INDEX IF NOT EXISTS idx_messages_processed_ts ON messages(processed, timestamp);
"""


MIGRATIONS = [
    # Add first_message_at column if missing (existing DBs)
    """
    ALTER TABLE conversations ADD COLUMN first_message_at DATETIME;
    """,
    # Backfill first_message_at from messages table for existing conversations
    """
    UPDATE conversations
    SET first_message_at = (
        SELECT datetime(MIN(timestamp), 'unixepoch')
        FROM messages
        WHERE messages.phone = conversations.phone
    )
    WHERE first_message_at IS NULL;
    """,
    # P3: Add hubspot_contact_id for cross-system reference
    """
    ALTER TABLE conversations ADD COLUMN hubspot_contact_id TEXT DEFAULT '';
    """,
    # Disable AI auto-reply per customer (big/VIP customers handled manually)
    """
    ALTER TABLE conversations ADD COLUMN ai_disabled INTEGER DEFAULT 0;
    """,
    # Flag big/key customers for priority handling
    """
    ALTER TABLE conversations ADD COLUMN is_big_customer INTEGER DEFAULT 0;
    """,
    # Customer size classification: big/medium/small (replaces is_big_customer)
    """
    ALTER TABLE conversations ADD COLUMN customer_size TEXT DEFAULT '';
    """,
    # Migrate existing is_big_customer=1 to customer_size='big'
    """
    UPDATE conversations SET customer_size = 'big' WHERE is_big_customer = 1 AND (customer_size = '' OR customer_size IS NULL);
    """,
    # P1b: Intent tags from LLM analysis
    """
    ALTER TABLE conversations ADD COLUMN intent_priority TEXT DEFAULT '';
    """,
    """
    ALTER TABLE conversations ADD COLUMN intent_tags TEXT DEFAULT '';
    """,
    # HubSpot enrichment cache — tier and product interest for AI context
    """
    ALTER TABLE conversations ADD COLUMN customer_tier TEXT DEFAULT '';
    """,
    """
    ALTER TABLE conversations ADD COLUMN product_interest TEXT DEFAULT '';
    """,
    # Cache location (country/city) from AI analysis
    """
    ALTER TABLE conversations ADD COLUMN location TEXT DEFAULT '';
    """,
    # Cache HubSpot customer_stage for stage-change detection
    """
    ALTER TABLE conversations ADD COLUMN customer_stage TEXT DEFAULT '';
    """,
    # Audit log for admin actions
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        action      TEXT NOT NULL,
        target      TEXT DEFAULT '',
        details     TEXT DEFAULT '',
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # Customer notes (separate from chat messages)
    """
    CREATE TABLE IF NOT EXISTS customer_notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        phone       TEXT NOT NULL,
        content     TEXT NOT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notes_phone ON customer_notes(phone);
    """,
    # Follow-up reminder date per customer
    """
    ALTER TABLE conversations ADD COLUMN next_followup TEXT DEFAULT '';
    """,
    # P1a: Customer actions table for daily reminders
    """
    CREATE TABLE IF NOT EXISTS customer_actions (
        phone           TEXT NOT NULL,
        action_date     TEXT NOT NULL,
        customer_name   TEXT DEFAULT '',
        today_action    TEXT DEFAULT '',
        tomorrow_action TEXT DEFAULT '',
        pending_customer TEXT DEFAULT '',
        priority        TEXT DEFAULT 'medium',
        summary         TEXT DEFAULT '',
        created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (phone, action_date)
    );
    """,
]


async def init_db() -> None:
    """Create tables if they don't exist, then run migrations."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(settings.db_path)) as db:
        # WAL mode: allows concurrent readers during writes (major perf win)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        await db.commit()

        # Run migrations (ignore errors for already-applied ones)
        for migration in MIGRATIONS:
            try:
                await db.executescript(migration)
                await db.commit()
            except Exception:
                pass  # Column already exists

    # Initialize retry queue table
    from app.store.retry_queue import init_retry_table
    await init_retry_table()

    logger.info("Database initialized at %s (WAL mode)", settings.db_path)


@asynccontextmanager
async def get_db():
    """Async context manager yielding an aiosqlite connection.

    Sets busy_timeout so concurrent writers wait instead of failing immediately.
    """
    db = await aiosqlite.connect(str(settings.db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout = 5000")
    try:
        yield db
    finally:
        await db.close()
