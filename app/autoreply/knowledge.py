"""Load and cache the product knowledge base and reply style from files."""

import logging
import os
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_cached_kb: str = ""
_cached_kb_mtime: float = 0.0
_cached_style: str = ""
_cached_style_mtime: float = 0.0


def _load_file(path: Path, cache: tuple[str, float]) -> tuple[str, float]:
    """Load a file if it changed since last read."""
    cached_text, cached_mtime = cache
    if not path.exists():
        logger.warning("File not found: %s", path)
        return "", 0.0
    mtime = os.path.getmtime(path)
    if mtime != cached_mtime or not cached_text:
        cached_text = path.read_text(encoding="utf-8")
        cached_mtime = mtime
        logger.info("Loaded %s (%d chars)", path.name, len(cached_text))
    return cached_text, cached_mtime


def get_knowledge_text() -> str:
    """Return the knowledge base content, reloading if the file changed."""
    global _cached_kb, _cached_kb_mtime
    _cached_kb, _cached_kb_mtime = _load_file(
        Path(settings.knowledge_base_path), (_cached_kb, _cached_kb_mtime)
    )
    return _cached_kb


def get_reply_style() -> str:
    """Return the reply style examples, reloading if the file changed."""
    global _cached_style, _cached_style_mtime
    style_path = Path(settings.knowledge_base_path).parent / "reply_style.md"
    _cached_style, _cached_style_mtime = _load_file(
        style_path, (_cached_style, _cached_style_mtime)
    )
    return _cached_style
