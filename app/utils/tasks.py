"""Safe fire-and-forget task helper.

Wraps asyncio.create_task so that:
1. Exceptions are logged instead of silently swallowed.
2. Strong references are kept to prevent GC before completion (Python 3.12+).
"""

import asyncio
import logging
from typing import Coroutine

logger = logging.getLogger(__name__)

# Strong reference set — prevents GC of running tasks (CPython 3.12+)
_background_tasks: set[asyncio.Task] = set()


def safe_task(coro: Coroutine, *, name: str = "") -> asyncio.Task:
    """Create a background task with exception logging and GC protection.

    Usage:
        safe_task(_hubspot_upsert(phone, name), name="hubspot-upsert")
    """
    task = asyncio.create_task(coro, name=name or None)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Background task %r failed: %s", t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_done)
    return task
