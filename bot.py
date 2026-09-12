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

import os
import re
import sqlite3
from contextlib import contextmanager
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
VOTE_CALLBACK_RE = re.compile(
    r"^vote:([1-9][0-9]{0,18}):(home|draw|away)$"
)

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
def db():
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(
            DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
        )
        try:
            yield PgConnWrapper(conn)
            conn.commit()
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


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
    buttons = [
        InlineKeyboardButton(m["home"], callback_data=f"vote:{m['id']}:home")
    ]
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
    await update.effective_message.reply_text(
        f"Your user ID: {user_id}\nThis chat ID: {chat.id}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update):
        return
    user = update.effective_user
    if get_player(user.id):
        await update.message.reply_text(f"You're already in, {user.first_name}!")
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO players (user_id, name) VALUES (?, ?)",
            (user.id, user.first_name),
        )
    await update.message.reply_text(
        f"Welcome to the World Cup pool, {user.first_name}!\n"
        "Use /vote to make your picks. /help for all commands."
    )


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
    if not home or not away:
        await update.message.reply_text(usage)
        return
    with db() as conn:
        new_id = conn.execute(
            "INSERT INTO matches (home, away, knockout) VALUES (?, ?, ?) "
            "RETURNING id",
            (home, away, knockout),
        ).fetchone()["id"]
    note = " (knockout — no draw option)" if knockout else ""
    await update.message.reply_text(
        f"Match #{new_id} added: {home} vs {away}{note}. Voting is open!"
    )


async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM matches ORDER BY id").fetchall()
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


async def next_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    if context.args and context.args[0].lower() != "game":
        await update.message.reply_text("Usage: /next game")
        return

    with db() as conn:
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
    with db() as conn:
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


async def invalid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    await update.callback_query.answer(
        "That button has expired. Send /vote for a new one.", show_alert=True
    )


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
    with db() as conn:
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
        conn.execute(
            "UPDATE matches SET result = ? WHERE id = ?", (outcome, match_id)
        )
        winners = conn.execute(
            "SELECT p.name FROM votes v JOIN players p ON p.user_id = v.user_id "
            "WHERE v.match_id = ? AND v.pick = ?",
            (match_id, outcome),
        ).fetchall()
    winner_names = ", ".join(w["name"] for w in winners) if winners else "nobody"
    await update.message.reply_text(
        f"{match_label(m)} — FINAL: {OUTCOME_LABELS[outcome]}\n"
        f"Correct picks: {winner_names}\n"
        "Check /leaderboard for updated rankings."
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_allowed_chat(update):
        return
    chat = update.effective_chat
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
            ORDER BY correct DESC, scored ASC, p.name
            """
        ).fetchall()
    if chat.type != "private":
        # In a group chat, only rank players who are members of that group.
        in_group = []
        for r in rows:
            try:
                member = await context.bot.get_chat_member(chat.id, r["user_id"])
            except TelegramError:
                continue
            if member.status not in ("left", "kicked"):
                in_group.append(r)
        rows = in_group
    if not rows:
        await update.message.reply_text("No players yet. Send /start to join!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 World Cup Prediction Leaderboard"]
    for i, r in enumerate(rows):
        rank = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{rank} {r['name']} — {r['correct']}/{r['scored']} correct")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------- main


def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit(
            "Set the WORLDCUP_BOT_TOKEN environment variable "
            "or add it to local.env."
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

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
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
