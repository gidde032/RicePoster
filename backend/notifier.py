"""Push-notification surface for unattended runs (FR-F3, DESIGN-scheduling.md §4).

When nobody is watching the UI, failures and unconfirmed results still need to
reach the maintainer's phone. The interface is deliberately pluggable — the
design doesn't commit to ntfy or any single service — and Slice 3's scheduler
consumes exactly `get_notifier() -> Notifier` / `await notifier.send(...) -> bool`
(cross-slice interface contract 2), so the shapes below are load-bearing.
"""

import re

import httpx

from backend import config
from backend.logging_setup import get_logger

_log = get_logger("notifier")

# ntfy topics are URL path segments concatenated straight into the request URL,
# so a slash/space/etc. would silently repoint the request. ntfy's own charset:
# letters, digits, '_' and '-', 1–64 chars.
_NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Notifier:
    """Send a push notification. Subclass for specific services.

    The base class is a no-op: notifications are disabled by default, so an
    unconfigured install (NOTIFY_SERVICE=none) never tries to reach the
    network."""

    async def send(self, title: str, body: str, priority: str = "default") -> bool:
        """Return True if sent successfully. Never raises."""
        return False


class NtfyNotifier(Notifier):
    """Push via ntfy.sh or a self-hosted ntfy instance.

    A message is a plain POST to `{server}/{topic}`; the title and priority ride
    in headers. Any failure (network down, non-200, bad topic) collapses to
    False — a broken notifier must never take down a posting run."""

    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        # rstrip so a server value with a trailing slash doesn't produce
        # "https://ntfy.sh//topic".
        self.url = f"{server.rstrip('/')}/{topic}"

    async def send(self, title: str, body: str, priority: str = "default") -> bool:
        try:
            # Bounded timeout: notification is best-effort and rides the posting
            # critical path — it must never stall a run for long. On timeout the
            # request raises, and the except below collapses it to False as usual.
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.url,
                    content=body.encode(),
                    headers={"Title": title, "Priority": priority},
                )
                return resp.status_code == 200
        except Exception as e:
            # No self.url here — the topic string grants send/subscribe access
            # on public ntfy.sh, so it never goes to a log. Type only.
            _log.warning(f"[notifier] ntfy send failed: {type(e).__name__}")
            return False


async def send_safe(notifier, title: str, body: str, priority: str = "default"):
    """Send one notification, swallowing anything it raises.

    The `Notifier.send` contract says "never raises and returns bool", but a
    contract is not an enforcement: a custom or subclassed notifier can still
    raise, and the caller has no way to know. So every send on a live path
    goes through here.

    This lived in `poster_browser` as a private `_push` and guarded the manual
    posting path only. `scheduler.py` awaited `notifier.send` bare at five
    sites — including inside its own crash handler, where a raise escaped
    `execute_batch` entirely and killed `scheduler_loop`, silently disabling
    all future scheduled posts (Phase 2 audit, finding #3). Moved here so both
    paths share one guard rather than one path re-deriving it.
    """
    try:
        ok = await notifier.send(title, body, priority)
        if not ok:
            _log.warning(f"[notify] send returned False: {title}")
        return ok
    except Exception as e:
        _log.warning(f"[notify] send raised (ignored): {type(e).__name__}")
        return False


def get_notifier() -> Notifier:
    """Return the notifier for the configured service, reading config at call
    time so tests can monkeypatch the config values.

    Falls back to the no-op base `Notifier` (never crashes) when:
      - NOTIFY_SERVICE is 'none' or unrecognised, or
      - NOTIFY_SERVICE=ntfy but NTFY_TOPIC is empty/malformed, or NTFY_SERVER
        has no http(s) scheme (misconfiguration should silence notifications,
        not take the server down — and, warned once here at factory time, is
        distinguishable from a network failure, which returns False at send).
    """
    service = config.NOTIFY_SERVICE
    if service == "ntfy":
        topic = config.NTFY_TOPIC.get_secret_value().strip()
        if not topic:
            _log.warning("[notifier] NOTIFY_SERVICE=ntfy but NTFY_TOPIC is empty — "
                  "notifications disabled")
            return Notifier()
        if not _NTFY_TOPIC_RE.match(topic):
            # Never print the topic value — it's a capability token that grants
            # send/subscribe on public ntfy.sh. Report the problem, not the value.
            _log.warning("[notifier] invalid NTFY_TOPIC format — notifications disabled")
            return Notifier()
        server = config.NTFY_SERVER
        if not (server.startswith("http://") or server.startswith("https://")):
            _log.warning(f"[notifier] invalid NTFY_SERVER '{server}' — must start with "
                  "http:// or https:// — notifications disabled")
            return Notifier()
        return NtfyNotifier(topic=topic, server=server)
    if service not in ("none", ""):
        _log.warning(f"[notifier] unknown NOTIFY_SERVICE='{service}' — "
              "notifications disabled")
    return Notifier()
