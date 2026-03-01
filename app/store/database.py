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
]


async def init_db() -> None:
    """Create tables if they don't exist, then run migrations."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(settings.db_path)) as db:
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

    logger.info("Database initialized at %s", settings.db_path)


@asynccontextmanager
async def get_db():
    """Async context manager yielding an aiosqlite connection."""
    db = await aiosqlite.connect(str(settings.db_path))
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()
