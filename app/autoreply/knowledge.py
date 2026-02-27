"""Load and cache the product knowledge base from a markdown file."""

import logging
import os
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_cached_text: str = ""
_cached_mtime: float = 0.0


def get_knowledge_text() -> str:
    """Return the knowledge base content, reloading if the file changed."""
    global _cached_text, _cached_mtime

    kb_path = Path(settings.knowledge_base_path)
    if not kb_path.exists():
        logger.warning("Knowledge base file not found: %s", kb_path)
        return ""

    mtime = os.path.getmtime(kb_path)
    if mtime != _cached_mtime or not _cached_text:
        _cached_text = kb_path.read_text(encoding="utf-8")
        _cached_mtime = mtime
        logger.info("Loaded knowledge base (%d chars) from %s", len(_cached_text), kb_path)

    return _cached_text
