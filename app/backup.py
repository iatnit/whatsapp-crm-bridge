"""SQLite daily backup with 7-day rotation."""

import logging
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(settings.data_dir) / "backups"
KEEP_DAYS = 7


async def run_backup() -> str | None:
    """Create a timestamped SQLite backup using the online backup API.

    Uses sqlite3.backup() which is safe even while the DB is being written to.
    Returns the backup file path on success, None on failure.
    """
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        cst = timezone(timedelta(hours=8))
        stamp = datetime.now(cst).strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"whatsapp_{stamp}.db"

        src = sqlite3.connect(str(settings.db_path))
        dst = sqlite3.connect(str(backup_path))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()

        size_mb = backup_path.stat().st_size / (1024 * 1024)
        logger.info("Backup created: %s (%.1f MB)", backup_path.name, size_mb)

        _cleanup_old()
        return str(backup_path)
    except Exception:
        logger.exception("Backup failed")
        return None


def _cleanup_old() -> int:
    """Remove backups older than KEEP_DAYS. Returns count deleted."""
    if not BACKUP_DIR.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    removed = 0
    for f in sorted(BACKUP_DIR.glob("whatsapp_*.db")):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            removed += 1
            logger.info("Removed old backup: %s", f.name)
    return removed
