import asyncio
import html
import logging
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

APP_NAME = "Manhwa Chapter Tracker"
BASE_URL = "https://asurascans.com"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/tracker.db")
CHECK_INTERVAL = max(300, int(os.getenv("CHECK_INTERVAL_SECONDS", "1200")))
ALLOWED_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
USER_AGENT = (
    "Mozilla/5.0 (compatible; ManhwaChapterTracker/1.0; "
    "+https://asurascans.com)"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(APP_NAME)

app = Flask(__name__)


def db_connect():
    folder = os.path.dirname(DATABASE_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                series_url TEXT NOT NULL,
                title TEXT NOT NULL,
                poster_url TEXT,
                latest_chapter TEXT,
                latest_url TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, series_url)
            )
        """)
        conn.commit()


def normalize_series_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if not raw_url.startswith(("https://", "http://")):
        raw_url = "https://" + raw_url
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if host not in {"asurascans.com", "www.asurascans.com", "asuracomic.net"}:
        raise ValueError("Sirf Asura Scans ka series URL add kar sakte ho.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in {"comics", "series"}:
        raise ValueError("Series page ka URL do, chapter page ka nahi.")
    slug = parts[1]
    if "chapter" in parts:
        raise ValueError("Chapter link nahi, poori Manhwa/series ka link do.")
    return f"{BASE_URL}/comics/{slug}"


def clean_title(value: str) -> str:
    value = re.sub(r"\s*[-|]\s*Asura Scans.*$", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def chapter_number(text: str):
    text = re.sub(r"\s+", " ", text or "").strip()
    patterns = [
        r"(?i)\bchapter\s*[-:#.]?\s*(\d+(?:\.\d+)?)",
        r"(?i)\bch\.?\s*[-:#.]?\s*(\d+(?:\.\d+)?)",
        r"(?i)\bepisode\s*[-:#.]?\s*(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def fetch_series(series_url: str) -> dict:
    """Fetch public series metadata and its newest visible chapter."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    response = requests.get(series_url, headers=headers, timeout=25)
    response.raise_for_status()
    final_url = response.url
    soup = BeautifulSoup(response.text, "html.parser")

    def meta(*names):
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find(
                "meta", attrs={"name": name}
            )
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = clean_title(meta("og:title", "twitter:title"))
    if not title:
        heading = soup.find(["h1", "h2"])
        title = clean_title(heading.get_text(" ", strip=True) if heading else "")
    if not title:
        title = final_url.rstrip("/").split("/")[-1].replace("-", " ").title()

    poster = meta("og:image", "twitter:image")
    if poster and poster.startswith("//"):
        poster = "https:" + poster

    # The Asura site may change its markup. Look for chapter anchors and
    # keep the first visible link with a chapter label/number.
    chapter_candidates = []
    seen = set()
    for anchor in soup.select('a[href*="/chapter/"]'):
        href = anchor.get("href", "").strip()
        label = anchor.get_text(" ", strip=True)
        if not href:
            continue
        if href.startswith("/"):
            href = BASE_URL + href
        elif href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        if parsed.hostname not in {"asurascans.com", "www.asurascans.com"}:
            continue
        if href in seen:
            continue
        seen.add(href)
        number = chapter_number(label)
        if not number:
            # Chapter slugs often end in a numeric chapter identifier.
            match = re.search(r"/chapter/(?:[^/?#]*?)(\d+(?:\.\d+)?)(?:/|$)", parsed.path, re.I)
            if match:
                number = match.group(1)
        if number:
            chapter_candidates.append({
                "label": label or f"Chapter {number}",
                "number": number,
                "url": href,
            })

    # Asura's latest-chapter list is rendered newest-first on its series page.
    latest = chapter_candidates[0] if chapter_candidates else None
    if not latest:
        # If the site changed markup, fail visibly rather than sending false updates.
        raise ValueError(
            "Series page mila, lekin chapter link parse nahi hua. "
            "Asura ne page layout badla ho sakta hai."
        )

    return {
        "title": title,
        "poster": poster,
        "latest_chapter": latest["label"],
        "latest_number": latest["number"],
        "latest_url": latest["url"],
        "series_url": final_url,
    }


def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    user = update.effective_user
    return bool(user and str(user.id) == ALLOWED_USER_ID)


async def deny_if_unauthorized(update: Update) -> bool:
    if is_authorized(update):
        return False
    if update.effective_message:
        await update.effective_message.reply_text(
            "Ye personal bot locked hai. Owner se access ke liye bolo."
        )
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_if_unauthorized(update):
        return
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"Namaste! 👋\n\n"
        f"Manhwa Chapter Tracker ready hai.\n"
        f"Tumhara Telegram User ID: `{user_id}`\n\n"
        f"Commands:\n"
        f"/add <Asura series URL> — Manhwa track karo\n"
        f"/list — tracked Manhwa dekho\n"
        f"/remove <number> — list se hatao\n"
        f"/check — abhi updates check karo\n"
        f"/help — help\n\n"
        f"Note: Private Asura bookmarks ko direct read karne ka official public API available nahi mila. "
        f"Is version mein tum series URLs ek baar add karoge; login password/cookie ki zarurat nahi.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_if_unauthorized(update):
        return
    await update.message.reply_text(
        "Use:\n"
        "/add https://asurascans.com/comics/series-name\n"
        "/list\n"
        "/remove 1\n"
        "/check\n\n"
        "Har 20 minute mein bot automatic check karta hai. "
        "New chapter milne par title, chapter, poster (agar Asura page deta hai) aur link bhejega."
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_if_unauthorized(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        await update.message.reply_text(
            "Series URL bhejo:\n/add https://asurascans.com/comics/series-name"
        )
        return
    try:
        series_url = normalize_series_url(raw)
        details = await asyncio.to_thread(fetch_series, series_url)
    except Exception as exc:
        await update.message.reply_text(f"Add nahi ho paya: {exc}")
        return

    chat_id = update.effective_chat.id
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT id FROM subscriptions WHERE chat_id=? AND series_url=?",
            (chat_id, details["series_url"]),
        ).fetchone()
        if existing:
            await update.message.reply_text("Ye Manhwa pehle se track ho raha hai.")
            return
        conn.execute(
            """INSERT INTO subscriptions
               (chat_id, series_url, title, poster_url, latest_chapter, latest_url)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                chat_id, details["series_url"], details["title"], details["poster"],
                details["latest_chapter"], details["latest_url"],
            ),
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Tracking shuru!\n\n"
        f"📖 {details['title']}\n"
        f"Latest chapter baseline: {details['latest_chapter']}\n\n"
        f"Ab iske baad aane wala naya chapter notify hoga. Purane chapters ke alerts nahi bhejunga."
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_if_unauthorized(update):
        return
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, title, latest_chapter FROM subscriptions WHERE chat_id=? ORDER BY id",
            (update.effective_chat.id,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("Abhi koi Manhwa track nahi ho raha. /add se shuru karo.")
        return
    lines = ["📚 *Tumhare tracked Manhwa:*"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['title']} — {row['latest_chapter']}  (remove: /remove {row['id']})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_if_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Use: /remove <ID> (ID /list mein dikhega)")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID number hona chahiye. /list dekho.")
        return
    with db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE id=? AND chat_id=?",
            (item_id, update.effective_chat.id),
        )
        conn.commit()
    await update.message.reply_text("Removed." if cur.rowcount else "Is ID ka Manhwa nahi mila.")


async def send_update(bot, chat_id: int, details: dict):
    title = html.escape(details["title"])
    chapter = html.escape(details["latest_chapter"])
    url = html.escape(details["latest_url"], quote=True)
    caption = (
        f"🆕 <b>New Chapter Released!</b>\n\n"
        f"📖 <b>{title}</b>\n"
        f"📚 <b>Chapter:</b> {chapter}\n\n"
        f'🔗 <a href="{url}">Read on Asura</a>'
    )
    poster = details.get("poster")
    try:
        if poster and poster.startswith("https://"):
            await bot.send_photo(
                chat_id=chat_id, photo=poster, caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Could not send poster; falling back to text")
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML)


async def check_one(bot, row, force=False):
    try:
        details = await asyncio.to_thread(fetch_series, row["series_url"])
        old = (row["latest_chapter"] or "").strip()
        new = (details["latest_chapter"] or "").strip()
        if old != new and old:
            await send_update(bot, row["chat_id"], details)
        with db_connect() as conn:
            conn.execute(
                """UPDATE subscriptions
                   SET title=?, poster_url=?, latest_chapter=?, latest_url=?
                   WHERE id=?""",
                (
                    details["title"], details["poster"], details["latest_chapter"],
                    details["latest_url"], row["id"],
                ),
            )
            conn.commit()
        return True, details["title"], (old != new and bool(old))
    except Exception as exc:
        log.warning("Check failed for %s: %s", row["series_url"], exc)
        return False, row["title"], False


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_if_unauthorized(update):
        return
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE chat_id=? ORDER BY id",
            (update.effective_chat.id,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("Pehle /add se Manhwa add karo.")
        return
    await update.message.reply_text(f"🔎 {len(rows)} Manhwa check kar raha hoon...")
    changed = 0
    failed = 0
    for row in rows:
        ok, _, updated = await check_one(context.bot, row)
        failed += not ok
        changed += updated
        await asyncio.sleep(1.2)
    await update.message.reply_text(
        f"Check complete. New chapters: {changed}. Failed checks: {failed}."
    )


async def periodic_scan(application: Application):
    while True:
        try:
            with db_connect() as conn:
                rows = conn.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
            for row in rows:
                await check_one(application.bot, row)
                await asyncio.sleep(1.5)  # be polite to the source website
        except Exception:
            log.exception("Periodic scan failed")
        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(application: Application):
    application.bot_data["scan_task"] = asyncio.create_task(periodic_scan(application))


async def post_shutdown(application: Application):
    task = application.bot_data.get("scan_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.get("/")
def home():
    return jsonify({
        "service": APP_NAME,
        "status": "ok",
        "bot_configured": bool(TOKEN),
        "check_interval_seconds": CHECK_INTERVAL,
    })


@app.get("/health")
def health():
    return "OK", 200


def run_bot():
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is missing; web health server will still run.")
        return
    # run_polling executes inside a dedicated background thread. Create and
    # register an event loop explicitly for Python 3.11+.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    init_db()
    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("check", check_command))
    log.info("Starting Telegram polling")
    application.run_polling(drop_pending_updates=True)


init_db()
if TOKEN:
    threading.Thread(target=run_bot, name="telegram-bot", daemon=True).start()
else:
    log.warning("Set TELEGRAM_BOT_TOKEN in Render environment variables.")


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
