"""
World Cup Prediction Bot
========================
A simple Telegram bot for a small group of friends to predict World Cup
match results and compete on a leaderboard.

Commands:
  /start            - register yourself as a player
  /addmatch         - add a match: /addmatch France vs Brazil
                      (append ' ko' for knockout)
  /matches          - list all matches and their status
  /vote             - vote on the current match (tap buttons)
  /next             - move voting to the next match
  /result           - record a result:  /result 3 home   (home/draw/away)
  /leaderboard      - current rankings, best to worst
  /help             - show commands

Runs in two modes, picked automatically:
  - Locally: long polling + a SQLite file (worldcup.db) next to this script.
  - On Render (or any host setting PORT + a public URL): Telegram webhooks +
    Postgres via the DATABASE_URL environment variable.

Matches are added manually with /addmatch as the tournament goes on.
"""

import asyncio
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------- config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_ENV_FILE = os.path.join(BASE_DIR, "local.env")


def load_local_env():
    if not os.path.exists(LOCAL_ENV_FILE):
        return {}

    values = {}
    with open(LOCAL_ENV_FILE, encoding="utf-8-sig") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise SystemExit(
                    f"{LOCAL_ENV_FILE}:{line_number} must use NAME=value syntax."
                )
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise SystemExit(f"{LOCAL_ENV_FILE}:{line_number} has an invalid name.")
            if key in values:
                raise SystemExit(f"{LOCAL_ENV_FILE}:{line_number} repeats {key}.")
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise SystemExit(
                        f"{LOCAL_ENV_FILE}:{line_number} has mismatched quotes."
                    )
                value = value[1:-1]
            values[key] = value
    return values


LOCAL_CONFIG = load_local_env()


def get_config(name):
    """Read a setting from the process environment or local.env."""
    value = os.environ.get(name)
    if value is None:
        value = LOCAL_CONFIG.get(name)
    return value.strip() if value else ""


def parse_id_set(name, raw_value, *, positive_only=False):
    """Parse a comma-separated list of Telegram user or chat IDs."""
    if not raw_value:
        return frozenset()

    ids = set()
    for item in re.split(r"[\s,]+", raw_value.strip()):
        try:
            item_id = int(item)
        except ValueError as exc:
            raise SystemExit(
                f"{name} must contain numeric IDs separated by commas."
            ) from exc
        if item_id == 0 or not -(2**63) <= item_id < 2**63:
            raise SystemExit(f"{name} contains an invalid Telegram ID.")
        if positive_only and item_id < 0:
            raise SystemExit(f"{name} must contain positive user IDs.")
        ids.add(item_id)
    return frozenset(ids)


BOT_TOKEN = get_config("WORLDCUP_BOT_TOKEN") or "PASTE_YOUR_TOKEN_HERE"
DATABASE_URL = get_config("DATABASE_URL")  # set -> Postgres, unset -> SQLite
ALLOWED_CHAT_IDS = parse_id_set(
    "WORLDCUP_ALLOWED_CHAT_IDS", get_config("WORLDCUP_ALLOWED_CHAT_IDS")
)
ADMIN_USER_IDS = parse_id_set(
    "WORLDCUP_ADMIN_USER_IDS",
    get_config("WORLDCUP_ADMIN_USER_IDS"),
    positive_only=True,
)
WEBHOOK_SECRET = get_config("WORLDCUP_WEBHOOK_SECRET")
WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
VOTE_CALLBACK_RE = re.compile(r"^vote:([1-9][0-9]{0,18}):(home|draw|away)$")

DB_FILE = os.path.join(BASE_DIR, "worldcup.db")

OUTCOME_LABELS = {"home": "Home win", "draw": "Draw", "away": "Away win"}
CURRENT_MATCH_KEY = "current_match_id"
ALLOWED_UPDATE_TYPES = ["message", "callback_query"]


def parse_webhook_config(port_value, public_url, secret):
    """Validate webhook settings and return (port, base URL), or None."""
    if not port_value and not public_url:
        return None
    if not port_value or not public_url:
        raise SystemExit("Webhook mode requires both PORT and a public URL.")

    try:
        port = int(port_value)
    except (TypeError, ValueError) as exc:
        raise SystemExit("PORT must be a number between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("PORT must be a number between 1 and 65535.")

    try:
        parsed_url = urlsplit(public_url)
        valid_host = bool(parsed_url.hostname)
    except ValueError:
        valid_host = False
        parsed_url = None
    if (
        not valid_host
        or parsed_url.scheme != "https"
        or parsed_url.username
        or parsed_url.password
        or parsed_url.path not in ("", "/")
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise SystemExit("The webhook public URL must be a plain HTTPS URL.")

    if not WEBHOOK_SECRET_RE.fullmatch(secret):
        raise SystemExit(
            "Set WORLDCUP_WEBHOOK_SECRET to 32-256 letters, numbers, "
            "underscores, or hyphens when using webhook mode."
        )
    return port, public_url.rstrip("/")


# Explicit finite work limits; these do not choose a financial budget.
def limit_setting(name, default, maximum):
    raw = get_config(name)
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer.") from exc
    if not 1 <= value <= maximum:
        raise SystemExit(f"{name} must be between 1 and {maximum}.")
    return value


GLOBAL_PER_MINUTE = limit_setting("WORLDCUP_GLOBAL_PER_MINUTE", 120, 10000)
USER_PER_MINUTE = limit_setting("WORLDCUP_USER_PER_MINUTE", 10, 1000)
CHAT_PER_MINUTE = limit_setting("WORLDCUP_CHAT_PER_MINUTE", 30, 1000)
MAX_PLAYERS = limit_setting("WORLDCUP_MAX_PLAYERS", 500, 10000)
MAX_MATCHES = limit_setting("WORLDCUP_MAX_MATCHES", 1000, 10000)
MAX_RECEIPTS = limit_setting("WORLDCUP_MAX_RECEIPTS", 100000, 1000000)
RECEIPT_SECONDS = 48 * 60 * 60
PAGE_SIZE = 20
MAX_PAGE = 500
MEMBERSHIP_SECONDS = 300
MAX_MEMBERSHIPS = 2000
DATABASE_TIMEOUT_SECONDS = 5
ALLOW_WEBHOOK_RESET = get_config("WORLDCUP_ALLOW_WEBHOOK_RESET").lower() == "true"
LOGGER = logging.getLogger(__name__)


class DuplicateUpdate(Exception):
    """The database already committed this Telegram mutation."""


class WorkLimitError(Exception):
    """An explicit finite operation or storage limit was reached."""


# ---------------------------------------------------------------- database

SCHEMA_SQLITE = """
    CREATE TABLE IF NOT EXISTS players (
        user_id INTEGER PRIMARY KEY,
        name    TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS matches (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        home     TEXT NOT NULL,
        away     TEXT NOT NULL,
        knockout INTEGER NOT NULL DEFAULT 0,
        result   TEXT
    );
    CREATE TABLE IF NOT EXISTS votes (
        match_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        pick     TEXT NOT NULL,
        PRIMARY KEY (match_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS processed_updates (
        update_id BIGINT PRIMARY KEY,
        processed_at BIGINT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS processed_updates_time ON processed_updates(processed_at);
    CREATE TABLE IF NOT EXISTS command_limits (
        scope TEXT PRIMARY KEY,
        window_start BIGINT NOT NULL,
        amount INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS membership_cache (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        visible INTEGER NOT NULL,
        expires_at BIGINT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    );

"""

SCHEMA_POSTGRES = """
    CREATE TABLE IF NOT EXISTS players (
        user_id BIGINT PRIMARY KEY,
        name    TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS matches (
        id       SERIAL PRIMARY KEY,
        home     TEXT NOT NULL,
        away     TEXT NOT NULL,
        knockout INTEGER NOT NULL DEFAULT 0,
        result   TEXT
    );
    CREATE TABLE IF NOT EXISTS votes (
        match_id INTEGER NOT NULL,
        user_id  BIGINT NOT NULL,
        pick     TEXT NOT NULL,
        PRIMARY KEY (match_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS processed_updates (
        update_id BIGINT PRIMARY KEY,
        processed_at BIGINT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS processed_updates_time ON processed_updates(processed_at);
    CREATE TABLE IF NOT EXISTS command_limits (
        scope TEXT PRIMARY KEY,
        window_start BIGINT NOT NULL,
        amount INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS membership_cache (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        visible INTEGER NOT NULL,
        expires_at BIGINT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    );

"""


class PgConnWrapper:
    """Gives a psycopg2 connection the same .execute()/.executemany() shape
    the rest of the code uses with sqlite3, translating ? placeholders."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executemany(self, sql, rows):
        cur = self._conn.cursor()
        cur.executemany(sql.replace("?", "%s"), rows)
        return cur

    executescript = execute


@contextmanager
def db(*, serialized=False, update=None):
    """One bounded transaction. Mutation receipts commit with their state changes."""
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
            connect_timeout=DATABASE_TIMEOUT_SECONDS,
            options="-c statement_timeout=5000 -c lock_timeout=5000",
        )
        wrapper = PgConnWrapper(conn)
    else:
        conn = sqlite3.connect(DB_FILE, timeout=DATABASE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + DATABASE_TIMEOUT_SECONDS
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        wrapper = conn
    try:
        if serialized or update is not None:
            if DATABASE_URL:
                # All consumers of this one pool use the same transaction lock.
                wrapper.execute("SELECT pg_advisory_xact_lock(902817401)")
            else:
                wrapper.execute("BEGIN IMMEDIATE")
        if update is not None:
            claim_update(wrapper, update)
        yield wrapper
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_update(conn, update):
    update_id = getattr(update, "update_id", None)
    if (
        not isinstance(update_id, int)
        or isinstance(update_id, bool)
        or not 0 <= update_id < 2**63
    ):
        raise WorkLimitError("Telegram update ID missing or invalid.")
    now = int(time.time())
    conn.execute(
        "DELETE FROM processed_updates WHERE processed_at < ?", (now - RECEIPT_SECONDS,)
    )
    if conn.execute(
        "SELECT update_id FROM processed_updates WHERE update_id = ?", (update_id,)
    ).fetchone():
        raise DuplicateUpdate()
    count = conn.execute("SELECT COUNT(*) AS n FROM processed_updates").fetchone()["n"]
    if count >= MAX_RECEIPTS:
        raise WorkLimitError("Update receipt capacity reached. Try later.")
    conn.execute(
        "INSERT INTO processed_updates (update_id, processed_at) VALUES (?, ?)",
        (update_id, now),
    )


def admit_command(chat_id, user_id):
    now = int(time.time())
    window = now // 60
    scopes = (
        ("global", GLOBAL_PER_MINUTE),
        (f"chat:{chat_id}", CHAT_PER_MINUTE),
        (f"user:{user_id}", USER_PER_MINUTE),
    )
    with db(serialized=True) as conn:
        conn.execute("DELETE FROM command_limits WHERE window_start < ?", (window - 1,))
        for scope, ceiling in scopes:
            row = conn.execute(
                "SELECT window_start, amount FROM command_limits WHERE scope = ?",
                (scope,),
            ).fetchone()
            if row and row["window_start"] == window and row["amount"] >= ceiling:
                return False
        for scope, _ in scopes:
            conn.execute(
                "INSERT INTO command_limits (scope, window_start, amount) VALUES (?, ?, 1) "
                "ON CONFLICT(scope) DO UPDATE SET window_start = excluded.window_start, "
                "amount = CASE WHEN command_limits.window_start = excluded.window_start "
                "THEN command_limits.amount + 1 ELSE 1 END",
                (scope, window),
            )
    return True


def bounded(*, admin=False):
    def decorate(handler):
        @wraps(handler)
        async def guarded(update, context):
            if not await (
                require_admin(update) if admin else require_allowed_chat(update)
            ):
                return
            user = update.effective_user
            if not user:
                await deny_access(
                    update, "Telegram did not include a user for this command."
                )
                return
            try:
                if not admit_command(update.effective_chat.id, user.id):
                    await deny_access(
                        update, "Work limit reached. Please retry next minute."
                    )
                    return
                async with asyncio.timeout(10):
                    return await handler(update, context)
            except DuplicateUpdate:
                await deny_access(update, "This update was already processed.")
            except WorkLimitError as exc:
                await deny_access(update, str(exc))

        return guarded

    return decorate


def page_number(context):
    args = context.args or []
    if not args:
        return 1
    if len(args) != 1 or not args[0].isdigit() or not 1 <= int(args[0]) <= MAX_PAGE:
        raise WorkLimitError(f"Use a page number from 1 to {MAX_PAGE}.")
    return int(args[0])


async def is_current_member(context, chat_id, user_id):
    now = int(time.time())
    with db() as conn:
        row = conn.execute(
            "SELECT visible, expires_at FROM membership_cache WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    if row and row["expires_at"] > now:
        return bool(row["visible"])
    try:
        member = await context.bot.get_chat_member(
            chat_id,
            user_id,
            read_timeout=5,
            write_timeout=5,
            connect_timeout=5,
            pool_timeout=5,
        )
        visible = member.status not in ("left", "kicked")
    except TelegramError:
        return False
    with db(serialized=True) as conn:
        conn.execute("DELETE FROM membership_cache WHERE expires_at <= ?", (now,))
        count = conn.execute("SELECT COUNT(*) AS n FROM membership_cache").fetchone()[
            "n"
        ]
        if count < MAX_MEMBERSHIPS or row:
            conn.execute(
                "INSERT INTO membership_cache (chat_id, user_id, visible, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET visible=excluded.visible, expires_at=excluded.expires_at",
                (chat_id, user_id, int(visible), now + MEMBERSHIP_SECONDS),
            )
    return visible


async def report_error(update, context):
    # Never let framework exception tracebacks log token-bearing URLs or database DSNs.
    LOGGER.error("Bot operation failed (%s)", type(context.error).__name__)
    if update is not None:
        await deny_access(update, "That operation failed. Please try again later.")


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA_POSTGRES if DATABASE_URL else SCHEMA_SQLITE)


# ---------------------------------------------------------------- helpers


def get_player(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_current_match(conn):
    current_id = get_setting(conn, CURRENT_MATCH_KEY)
    if current_id:
        try:
            current = conn.execute(
                "SELECT * FROM matches WHERE id = ?", (int(current_id),)
            ).fetchone()
            if current:
                return current
        except ValueError:
            pass

    first_open = conn.execute(
        "SELECT * FROM matches WHERE result IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if first_open:
        set_setting(conn, CURRENT_MATCH_KEY, first_open["id"])
    return first_open


def get_next_open_match(conn, current_id):
    next_match = conn.execute(
        "SELECT * FROM matches WHERE result IS NULL AND id > ? ORDER BY id LIMIT 1",
        (current_id,),
    ).fetchone()
    if next_match:
        return next_match
    return conn.execute(
        "SELECT * FROM matches WHERE result IS NULL ORDER BY id LIMIT 1"
    ).fetchone()


def vote_keyboard(m):
    buttons = [InlineKeyboardButton(m["home"], callback_data=f"vote:{m['id']}:home")]
    if not m["knockout"]:
        buttons.append(
            InlineKeyboardButton("Draw", callback_data=f"vote:{m['id']}:draw")
        )
    buttons.append(
        InlineKeyboardButton(m["away"], callback_data=f"vote:{m['id']}:away")
    )
    return InlineKeyboardMarkup([buttons])


def match_label(m):
    return f"#{m['id']} {m['home']} vs {m['away']}"


async def deny_access(update, message):
    """Send an access error without leaving callback buttons spinning."""
    if update.callback_query:
        await update.callback_query.answer(message, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(message)


async def require_allowed_chat(update):
    chat = update.effective_chat
    if chat and chat.id in ALLOWED_CHAT_IDS:
        return True
    await deny_access(
        update,
        "This bot is private. Use /whereami and ask the owner to allow this chat.",
    )
    return False


async def require_admin(update):
    if not await require_allowed_chat(update):
        return False
    user = update.effective_user
    if user and user.id in ADMIN_USER_IDS:
        return True
    await deny_access(update, "Only a configured pool admin can do that.")
    return False


async def require_user(update):
    if not await require_allowed_chat(update):
        return False
    if update.effective_user:
        return True
    await deny_access(update, "Telegram did not include a user for this command.")
    return False


# ---------------------------------------------------------------- commands


async def whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show IDs needed for the local allowlist configuration."""
    user = update.effective_user
    chat = update.effective_chat
    if not chat or not update.effective_message:
        return
    user_id = user.id if user else "unavailable"
    if not admit_command(chat.id, user.id if user else 0):
        return
    await update.effective_message.reply_text(
        f"Your user ID: {user_id}\nThis chat ID: {chat.id}"
    )


@bounded()
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update):
        return
    user = update.effective_user
    if get_player(user.id):
        await update.message.reply_text(f"You're already in, {user.first_name}!")
        return
    with db(update=update) as conn:
        if (
            conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
            >= MAX_PLAYERS
        ):
            raise WorkLimitError(
                "Player capacity reached. Ask the owner to review pool retention."
            )
        conn.execute(
            "INSERT INTO players (user_id, name) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING",
            (user.id, user.first_name),
        )
    await update.message.reply_text(
        f"Welcome to the World Cup pool, {user.first_name}!\n"
        "Use /vote to make your picks. /help for all commands."
    )


@bounded()
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    await update.message.reply_text(
        "/start - join the pool\n"
        "/addmatch France vs Brazil - admins can add a match (add ' ko' for knockout)\n"
        "/matches - list all matches\n"
        "/vote - vote on the current match\n"
        "/next game - admins can open voting for the next match\n"
        "/result 3 home - admins can record a result (home/draw/away)\n"
        "/leaderboard - rankings, best to worst\n"
        "/whereami - show your user ID and this chat's ID"
    )


@bounded(admin=True)
async def addmatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    usage = (
        "Usage: /addmatch France vs Brazil\n"
        "Knockout match (no draw option): /addmatch France vs Brazil ko"
    )
    text = " ".join(context.args)
    knockout = 0
    if text.lower().endswith((" ko", " knockout")):
        knockout = 1
        text = text.rsplit(" ", 1)[0]
    if " vs " not in text:
        await update.message.reply_text(usage)
        return
    home, away = (part.strip() for part in text.split(" vs ", 1))
    if not home or not away or len(home) > 80 or len(away) > 80:
        await update.message.reply_text(usage)
        return
    with db(update=update) as conn:
        if (
            conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]
            >= MAX_MATCHES
        ):
            raise WorkLimitError(
                "Match capacity reached. Ask the owner to export/archive the pool."
            )
        new_id = conn.execute(
            "INSERT INTO matches (home, away, knockout) VALUES (?, ?, ?) RETURNING id",
            (home, away, knockout),
        ).fetchone()["id"]
    note = " (knockout — no draw option)" if knockout else ""
    await update.message.reply_text(
        f"Match #{new_id} added: {home} vs {away}{note}. Voting is open!"
    )


@bounded()
async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    page = page_number(context)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM matches ORDER BY id LIMIT ? OFFSET ?",
            (PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
        ).fetchall()
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    if not rows:
        await update.message.reply_text("No matches yet. Add one with /addmatch.")
        return
    lines = []
    for m in rows:
        status = (
            f"FINAL: {OUTCOME_LABELS[m['result']]}" if m["result"] else "open for votes"
        )
        lines.append(f"{match_label(m)} — {status}")
    # Telegram messages cap at 4096 chars; send in chunks
    chunk = []
    length = 0
    for line in lines:
        if length + len(line) + 1 > 3800:
            await update.message.reply_text("\n".join(chunk))
            chunk, length = [], 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        await update.message.reply_text("\n".join(chunk))
    if has_more:
        await update.message.reply_text(f"More matches: /matches {page + 1}")


@bounded()
async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update):
        return
    user = update.effective_user
    if not get_player(user.id):
        await update.message.reply_text(
            "You're not registered yet — send /start first."
        )
        return
    with db() as conn:
        current_match = get_current_match(conn)
        picks = {
            row["match_id"]: row["pick"]
            for row in conn.execute(
                "SELECT match_id, pick FROM votes WHERE user_id = ?", (user.id,)
            ).fetchall()
        }
    if not current_match:
        await update.message.reply_text("No open matches to vote on right now.")
        return

    if current_match["result"]:
        await update.message.reply_text(
            f"{match_label(current_match)} is already final. "
            "Use /next game when you're ready to open voting for the next match."
        )
        return

    current = picks.get(current_match["id"])
    note = f" (your pick: {OUTCOME_LABELS[current]})" if current else ""
    await update.message.reply_text(
        f"{match_label(current_match)}{note}",
        reply_markup=vote_keyboard(current_match),
    )


@bounded(admin=True)
async def next_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if context.args and context.args[0].lower() != "game":
        await update.message.reply_text("Usage: /next game")
        return

    with db(update=update) as conn:
        current_match = get_current_match(conn)
        if not current_match:
            await update.message.reply_text("No open matches to vote on right now.")
            return
        next_match = get_next_open_match(conn, current_match["id"])
        if not next_match:
            await update.message.reply_text("No open matches to vote on right now.")
            return
        if next_match["id"] == current_match["id"]:
            await update.message.reply_text(
                f"{match_label(current_match)} is the only open match right now."
            )
            return
        set_setting(conn, CURRENT_MATCH_KEY, next_match["id"])

    await update.message.reply_text(
        f"Voting is now open for {match_label(next_match)}.\n"
        "Use /vote to make or change your pick."
    )


@bounded()
async def vote_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await require_allowed_chat(update):
        return
    user = query.from_user
    if not user:
        await query.answer(
            "Telegram did not include a user for this vote.", show_alert=True
        )
        return
    if not get_player(user.id):
        await query.answer("Send /start first to join the pool.", show_alert=True)
        return
    callback_match = (
        VOTE_CALLBACK_RE.fullmatch(query.data) if isinstance(query.data, str) else None
    )
    if not callback_match:
        await query.answer("That vote button is invalid.", show_alert=True)
        return
    match_id = int(callback_match.group(1))
    pick = callback_match.group(2)
    with db(update=update) as conn:
        m = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        if not m:
            await query.answer("That match no longer exists.", show_alert=True)
            await query.edit_message_text("That match no longer exists.")
            return
        if m["result"]:
            await query.answer("Too late — the result is already in!", show_alert=True)
            return
        if pick == "draw" and m["knockout"]:
            await query.answer("Knockout match — no draws!", show_alert=True)
            return
        conn.execute(
            "INSERT INTO votes (match_id, user_id, pick) VALUES (?, ?, ?) "
            "ON CONFLICT(match_id, user_id) DO UPDATE SET pick = excluded.pick",
            (match_id, user.id, pick),
        )
    await query.answer("Pick saved.")
    pick_text = m["home"] if pick == "home" else m["away"] if pick == "away" else "Draw"
    await query.edit_message_text(
        f"{match_label(m)}\n{user.first_name} picked: {pick_text} ✅"
    )


@bounded()
async def invalid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    await update.callback_query.answer(
        "That button has expired. Send /vote for a new one.", show_alert=True
    )


@bounded(admin=True)
async def result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if len(context.args) != 2 or context.args[1].lower() not in OUTCOME_LABELS:
        await update.message.reply_text("Usage: /result <match_id> <home|draw|away>")
        return
    match_id, outcome = context.args[0].lstrip("#"), context.args[1].lower()
    if not match_id.isdigit():
        await update.message.reply_text("Usage: /result <match_id> <home|draw|away>")
        return
    match_id = int(match_id)
    with db(update=update) as conn:
        m = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        if not m:
            await update.message.reply_text(
                f"No match #{match_id} found. See /matches."
            )
            return
        if outcome == "draw" and m["knockout"]:
            await update.message.reply_text(
                "That's a knockout match — record the team that advanced "
                "(home or away), even if it went to penalties."
            )
            return
        conn.execute("UPDATE matches SET result = ? WHERE id = ?", (outcome, match_id))
        winners = conn.execute(
            "SELECT p.name FROM votes v JOIN players p ON p.user_id = v.user_id "
            "WHERE v.match_id = ? AND v.pick = ? LIMIT 21",
            (match_id, outcome),
        ).fetchall()
    winner_names = ", ".join(w["name"] for w in winners[:20]) if winners else "nobody"
    if len(winners) > 20:
        winner_names += ", and more (see /leaderboard)"
    await update.message.reply_text(
        f"{match_label(m)} — FINAL: {OUTCOME_LABELS[outcome]}\n"
        f"Correct picks: {winner_names}\n"
        "Check /leaderboard for updated rankings."
    )


@bounded()
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    chat = update.effective_chat
    page = page_number(context)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.user_id, p.name,
                   COUNT(CASE WHEN v.pick = m.result THEN 1 END) AS correct,
                   COUNT(CASE WHEN m.result IS NOT NULL THEN 1 END) AS scored
            FROM players p
            LEFT JOIN votes v ON v.user_id = p.user_id
            LEFT JOIN matches m ON m.id = v.match_id
            GROUP BY p.user_id, p.name
            ORDER BY correct DESC, scored ASC, p.name, p.user_id
            LIMIT ? OFFSET ?
            """,
            (PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
        ).fetchall()
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    if chat.type != "private":
        # In a group chat, only rank players who are members of that group.
        in_group = []
        for row in rows:
            if await is_current_member(context, chat.id, row["user_id"]):
                in_group.append(row)
        rows = in_group
    if not rows:
        await update.message.reply_text(
            "No visible players on this page. Use /start to join, or request another leaderboard page."
        )
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 World Cup Prediction Leaderboard"]
    for i, r in enumerate(rows, start=(page - 1) * PAGE_SIZE):
        rank = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{rank} {r['name']} — {r['correct']}/{r['scored']} correct")
    if has_more:
        lines.append(f"Next pool page: /leaderboard {page + 1}")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------- main


async def guard_delivery_mode(bot, expected_url=""):
    info = await bot.get_webhook_info()
    if info.url and info.url != expected_url and not ALLOW_WEBHOOK_RESET:
        raise RuntimeError(
            "A different webhook is configured. Use a separate test bot or explicitly set WORLDCUP_ALLOW_WEBHOOK_RESET=true for an operator-approved mode switch."
        )


def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE" or BOT_TOKEN.startswith("replace-with-"):
        raise SystemExit(
            "Set the WORLDCUP_BOT_TOKEN environment variable or add it to local.env."
        )

    port = os.environ.get("PORT")
    public_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
    webhook_config = parse_webhook_config(port, public_url, WEBHOOK_SECRET)
    if webhook_config and not DATABASE_URL:
        raise SystemExit("DATABASE_URL is required in webhook mode.")

    if not ALLOWED_CHAT_IDS:
        print(
            "No chats are allowed yet. Use /whereami, update the allowlist, "
            "and restart."
        )
    if not ADMIN_USER_IDS:
        print("No pool admins are configured; management commands are disabled.")

    mode = get_config("WORLDCUP_MODE") or "auto"
    if mode not in ("auto", "polling", "webhook"):
        raise SystemExit("WORLDCUP_MODE must be auto, polling, or webhook.")
    if mode == "webhook" and not webhook_config:
        raise SystemExit("Webhook mode requires PORT and a public URL.")
    if mode == "polling" and webhook_config:
        raise SystemExit(
            "Remove hosted PORT/public URL settings before choosing polling."
        )
    init_db()
    expected_url = f"{webhook_config[1]}/telegram" if webhook_config else ""

    async def preflight(application):
        await guard_delivery_mode(application.bot, expected_url)

    app = Application.builder().token(BOT_TOKEN).post_init(preflight).build()
    message_updates = filters.UpdateType.MESSAGE
    app.add_handler(CommandHandler("whereami", whereami, filters=message_updates))
    app.add_handler(CommandHandler("start", start, filters=message_updates))
    app.add_handler(CommandHandler("help", help_cmd, filters=message_updates))
    app.add_handler(CommandHandler("addmatch", addmatch, filters=message_updates))
    app.add_handler(CommandHandler("matches", matches_cmd, filters=message_updates))
    app.add_handler(CommandHandler("vote", vote, filters=message_updates))
    app.add_handler(CommandHandler("next", next_game, filters=message_updates))
    app.add_handler(CommandHandler("nextgame", next_game, filters=message_updates))
    app.add_handler(CommandHandler("next_game", next_game, filters=message_updates))
    app.add_handler(CommandHandler("result", result, filters=message_updates))
    app.add_handler(CommandHandler("leaderboard", leaderboard, filters=message_updates))
    app.add_handler(CallbackQueryHandler(vote_button, pattern=VOTE_CALLBACK_RE))
    app.add_handler(CallbackQueryHandler(invalid_button))
    app.add_error_handler(report_error)

    if webhook_config:
        webhook_port, webhook_base_url = webhook_config
        print(f"World Cup bot running in webhook mode at {webhook_base_url}.")
        app.run_webhook(
            listen="0.0.0.0",
            port=webhook_port,
            url_path="telegram",
            webhook_url=f"{webhook_base_url}/telegram",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=ALLOWED_UPDATE_TYPES,
        )
    else:
        print("World Cup bot running in polling mode. Press Ctrl+C to stop.")
        app.run_polling(allowed_updates=ALLOWED_UPDATE_TYPES)


if __name__ == "__main__":
    main()
