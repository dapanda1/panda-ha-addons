"""Telegram bot integration for Plex Library Index.

Self-contained Telegram client. Outbound notifications via send_message().
Inbound commands via long-polling getUpdates loop in run_polling_loop().

This module is scope-limited on purpose. It only handles Plex Library Index
queries — search, random pick, status. Anything else is silently ignored so
a separate full-purpose Telegram bot can coexist (different bot token, or
the same bot routed through HA automations for non-Plex intents).
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from difflib import get_close_matches

import aiohttp

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30  # long-poll seconds


def _sanitize_text(s: str, limit: int = 100) -> str:
    """Trim and bound user input so we don't echo huge payloads back."""
    if not s:
        return ""
    s = s.strip()
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def _escape_md(text: str) -> str:
    """Escape Telegram MarkdownV2 special chars. We use plain text mode by
    default to dodge this complexity, but keep the helper around for future
    rich formatting."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(text or ""))


class TelegramClient:
    def __init__(self, token: str, allowed_chat_ids=None):
        self.token = token
        # Coerce to set of ints. Empty list = allow nobody.
        self.allowed = set()
        for cid in allowed_chat_ids or []:
            try:
                self.allowed.add(int(cid))
            except (ValueError, TypeError):
                pass
        self._offset = 0
        self._session = None
        self._bot_username = None

    async def _api(self, method: str, params=None, timeout: int = 15):
        """Call Telegram API. Returns (ok, result_or_error)."""
        if not self.token:
            return False, "no token"
        url = TELEGRAM_API.format(token=self.token, method=method)
        try:
            async with self._session.post(
                url, json=params or {}, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as r:
                body = await r.json()
                if body.get("ok"):
                    return True, body.get("result")
                return False, body.get("description", f"HTTP {r.status}")
        except asyncio.TimeoutError:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    async def open(self):
        self._session = aiohttp.ClientSession()
        ok, me = await self._api("getMe", timeout=10)
        if ok and isinstance(me, dict):
            self._bot_username = me.get("username")
            logging.info(f"telegram: connected as @{self._bot_username}")
        else:
            logging.warning(f"telegram: getMe failed: {me}")

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    def is_allowed(self, chat_id) -> bool:
        if not self.allowed:
            return False
        try:
            return int(chat_id) in self.allowed
        except (ValueError, TypeError):
            return False

    async def send_message(self, chat_id, text: str, *, silent: bool = False):
        if not self._session:
            logging.warning("telegram: send_message called before open()")
            return False
        # Plain text mode — no Markdown parsing to keep things robust.
        ok, result = await self._api(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": text[:4000],  # Telegram limit is 4096
                "disable_notification": silent,
                "disable_web_page_preview": True,
            },
        )
        if not ok:
            logging.warning(f"telegram: send to {chat_id} failed: {result}")
        return ok

    async def broadcast(self, text: str, *, silent: bool = False):
        """Send to every allowed chat. Returns count of successful sends."""
        n = 0
        for cid in self.allowed:
            if await self.send_message(cid, text, silent=silent):
                n += 1
        return n

    async def get_updates(self):
        """Long-poll for one batch of updates. Returns list of updates."""
        ok, result = await self._api(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message"],
            },
            timeout=POLL_TIMEOUT + 10,
        )
        if not ok:
            logging.warning(f"telegram: getUpdates error: {result}")
            return []
        updates = result or []
        if updates:
            self._offset = max(u["update_id"] for u in updates) + 1
        return updates


# ---- Library-aware command handling ----

def _normalize(s):
    return (s or "").strip().lower()


def search_library(payload: dict, query: str, limit: int = 5):
    """Substring + fuzzy-fallback search across movie and show titles.

    Returns a list of items (mixed movies and shows), capped at `limit`.
    """
    q = _normalize(query)
    if not q or not payload:
        return []
    items = (payload.get("movies") or []) + (payload.get("shows") or [])
    tokens = q.split()

    # Substring AND match first
    hits = []
    for it in items:
        hay = " ".join([
            (it.get("title") or "").lower(),
            (it.get("original_title") or "").lower(),
            str(it.get("year") or ""),
        ])
        if all(tok in hay for tok in tokens):
            hits.append(it)

    # If nothing, try fuzzy on title only
    if not hits:
        titles_map = {(it.get("title") or "").lower(): it for it in items if it.get("title")}
        close = get_close_matches(q, list(titles_map.keys()), n=limit, cutoff=0.62)
        hits = [titles_map[t] for t in close]

    return hits[:limit]


def random_from_library(payload: dict):
    import random
    items = (payload.get("movies") or []) + (payload.get("shows") or [])
    if not items:
        return None
    return random.choice(items)


def format_item_brief(item):
    if not item:
        return "no match"
    t = item.get("type", "?")
    title = item.get("title", "(untitled)")
    year = item.get("year")
    lib = item.get("library", "")
    res = item.get("resolution")
    extras = []
    if res:
        extras.append(res)
    if t == "show":
        sc = item.get("season_count") or 0
        ec = item.get("episode_count") or 0
        extras.append(f"{sc}s/{ec}ep")
    elif t == "movie":
        dur = item.get("duration_min")
        if dur:
            extras.append(f"{dur}m")
    head = f"{title}" + (f" ({year})" if year else "")
    tail = " · ".join(extras) if extras else ""
    return f"{'🎬' if t == 'movie' else '📺'} {head}\n   {lib}" + (f" · {tail}" if tail else "")


def format_search_results(query: str, hits: list) -> str:
    if not hits:
        return f"No results for “{_sanitize_text(query)}”."
    lines = [f"Results for “{_sanitize_text(query)}” ({len(hits)}):", ""]
    for h in hits:
        lines.append(format_item_brief(h))
    return "\n".join(lines)


def format_status(state: dict, payload: dict) -> str:
    counts = state.get("counts") or {}
    last = state.get("last_scan")
    scanning = state.get("scanning")
    server = (payload or {}).get("server", {}) if payload else {}
    lines = ["📚 Plex Library Index"]
    if server.get("name"):
        lines.append(f"Server: {server['name']} (Plex {server.get('version','?')})")
    lines.append(f"Movies: {counts.get('movies', 0)}")
    lines.append(f"Shows: {counts.get('shows', 0)} ({counts.get('seasons', 0)} seasons)")
    if scanning:
        lines.append("⏳ Scan in progress…")
    elif last:
        try:
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            ago = datetime.now(timezone.utc) - dt
            mins = int(ago.total_seconds() // 60)
            if mins < 60:
                ago_str = f"{mins}m ago"
            elif mins < 1440:
                ago_str = f"{mins // 60}h ago"
            else:
                ago_str = f"{mins // 1440}d ago"
            lines.append(f"Last scan: {ago_str}")
        except Exception:
            lines.append(f"Last scan: {last}")
    if state.get("last_error"):
        lines.append(f"⚠️ Last error: {state['last_error']}")
    return "\n".join(lines)


HELP_TEXT = (
    "📚 Plex Library Index bot\n\n"
    "Commands:\n"
    "  /search <query> — search movies and shows (also: just send any text)\n"
    "  /random — pick a random title\n"
    "  /status — index health and counts\n"
    "  /help — this message\n\n"
    "Or just send any text to search the library."
)


async def handle_update(update: dict, *, client: TelegramClient,
                        get_library, get_state, log_fn=logging.info):
    """Dispatch one Telegram update. `get_library` and `get_state` are
    callables returning the latest payload dict / state dict from the
    server module."""
    msg = update.get("message")
    if not msg:
        return  # only handle messages, not edits / channel posts / etc.
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text_raw = msg.get("text") or ""
    text = text_raw.strip()
    user = msg.get("from", {}).get("username") or msg.get("from", {}).get("first_name", "?")

    if not client.is_allowed(chat_id):
        log_fn(f"telegram: rejected message from chat_id={chat_id} user={user}")
        # Don't reply at all — silent to non-allowed users.
        return

    log_fn(f"telegram: from {user} ({chat_id}): {_sanitize_text(text, 60)!r}")

    if not text:
        return

    lowered = text.lower()

    # Slash commands: anything we don't recognize is intentionally ignored
    # so a future general-purpose bot using the same chat can handle others.
    if text.startswith("/"):
        # Telegram appends @botusername for group commands; strip it
        cmd, _, args = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        args = args.strip()

        if cmd in ("/start", "/help"):
            await client.send_message(chat_id, HELP_TEXT)
            return
        if cmd == "/status":
            payload = get_library()
            state = get_state()
            await client.send_message(chat_id, format_status(state, payload))
            return
        if cmd == "/random":
            payload = get_library()
            item = random_from_library(payload)
            await client.send_message(
                chat_id,
                "🎲 Random pick:\n\n" + format_item_brief(item) if item else "Library is empty.",
            )
            return
        if cmd == "/search":
            if not args:
                await client.send_message(chat_id, "Usage: /search <query>")
                return
            payload = get_library()
            hits = search_library(payload, args)
            await client.send_message(chat_id, format_search_results(args, hits))
            return
        # Unknown slash command — leave for other bots / automations.
        return

    # Free text → library search
    payload = get_library()
    hits = search_library(payload, text)
    await client.send_message(chat_id, format_search_results(text, hits))


async def run_polling_loop(*, get_options, get_library, get_state,
                           on_ready=None, stop_event: asyncio.Event):
    """Long-poll Telegram, dispatching messages. Reads config from
    `get_options()` at startup; restarts polling if the token changes.

    Cleanly exits when `stop_event` is set.
    """
    backoff = 5
    client = None
    current_token = None
    current_allowed = None

    while not stop_event.is_set():
        opts = get_options()
        if not opts.get("telegram_enabled"):
            await asyncio.sleep(10)
            continue

        token = opts.get("telegram_bot_token") or ""
        allowed = opts.get("telegram_allowed_chat_ids") or []
        if not token:
            logging.warning("telegram: enabled but no bot token configured")
            await asyncio.sleep(30)
            continue

        # (Re)create client if token or allowed list changed
        if not client or token != current_token or allowed != current_allowed:
            if client:
                await client.close()
            client = TelegramClient(token, allowed)
            try:
                await client.open()
            except Exception as e:
                logging.warning(f"telegram: failed to open session: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            current_token = token
            current_allowed = allowed
            if on_ready:
                try:
                    await on_ready(client)
                except Exception as e:
                    logging.warning(f"telegram on_ready handler errored: {e}")

        # Poll one batch
        try:
            updates = await client.get_updates()
            backoff = 5
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning(f"telegram polling error: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        if not opts.get("telegram_answer_commands"):
            # Drain updates without acting on them, so when re-enabled we
            # don't replay a backlog.
            continue

        for update in updates:
            try:
                await handle_update(
                    update, client=client,
                    get_library=get_library, get_state=get_state,
                )
            except Exception as e:
                logging.exception(f"telegram: handler error: {e}")

    if client:
        await client.close()
    logging.info("telegram: polling loop exited")
