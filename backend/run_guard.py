"""Shared single-run guard: only one posting run (manual or scheduled) at a time.

Extracted from main.py so scheduler.py can use it without importing main
(circular import). Single event loop, no await between check and set, so
the flag is race-safe in asyncio.
"""

_post_running = False


def try_acquire() -> bool:
    """Attempt to acquire the posting guard. Returns True if acquired."""
    global _post_running
    if _post_running:
        return False
    _post_running = True
    return True


def release():
    """Release the posting guard."""
    global _post_running
    _post_running = False


def is_running() -> bool:
    """Check if a posting run is active (read-only)."""
    return _post_running
