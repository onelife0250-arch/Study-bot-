"""
StudyBot — bot.py
Telegram Study Bot (Classes 9-12) — single-file runnable template.

Features:
- Class -> Category -> Subject -> Chapter -> Items (PDFs, notes)
- Admin upload via caption: class|category|subject|chapter|title|premium
- Quizzes (admin can add)
- Premium via UPI (manual) and Razorpay Payment Links + webhook (auto-activate)
- SQLite persistence
- FastAPI webhook endpoint for Razorpay
- Designed for deployment on Railway / Render / local testing (ngrok)
"""

import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import List, Optional

# Telegram
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Poll,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Optional libs
try:
    import razorpay
    RAZORPAY_AVAILABLE = True
except Exception:
    RAZORPAY_AVAILABLE = False

# Webhook server
try:
    from fastapi import FastAPI, Request
    import uvicorn
    FASTAPI_AVAILABLE = True
except Exception:
    FASTAPI_AVAILABLE = False

# -------------------- CONFIG (ENV) --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "BOT_TOKEN")
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
PAYMENT_UPI_ID = os.getenv("PAYMENT_UPI_ID", "default@upi")
PAYMENT_NOTE = os.getenv("PAYMENT_NOTE", "StudyBot Premium")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "set_a_random_secret")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # e.g. https://your-app.railway.app
PORT = int(os.getenv("PORT", "8080"))

CLASSES = ["9", "10", "11", "12"]
CATEGORIES = ["Short Notes", "PYQ", "Sample Papers", "Handwritten Notes", "Test Series", "Quizzes"]

PAGE_SIZE = 8
DB_PATH = "studybot.db"

# Razorpay plans (amount in paise)
PLANS = {
    "1m": {"months": 1, "amount": 99_00, "label": "1 Month ₹99"},
    "3m": {"months": 3, "amount": 249_00, "label": "3 Months ₹249"},
    "12m": {"months": 12, "amount": 699_00, "label": "12 Months ₹699"},
}

# -------------------- DB (SQLite) --------------------
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE,
    name TEXT,
    is_premium INTEGER DEFAULT 0,
    joined_at TEXT
);

CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_num TEXT,
    category TEXT,
    subject TEXT,
    chapter TEXT,
    title TEXT,
    file_id TEXT,
    premium INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS quizzes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_num TEXT,
    subject TEXT,
    chapter TEXT,
    question TEXT,
    option1 TEXT,
    option2 TEXT,
    option3 TEXT,
    option4 TEXT,
    correct_index INTEGER,
    premium INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    txn_id TEXT,
    plan TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    quiz_id INTEGER,
    chosen_index INTEGER,
    correct INTEGER,
    timestamp TEXT
);
"""

with db() as con:
    con.executescript(SCHEMA)
    con.commit()

# -------------------- HELPERS --------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def ensure_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (tg_id, name, is_premium, joined_at) VALUES (?,?,0,?)",
            (update.effective_user.id, update.effective_user.full_name, datetime.utcnow().isoformat()),
        )
        con.commit()

def user_is_premium_sync(tg_id: int) -> bool:
    with db() as con:
        cur = con.execute("SELECT is_premium FROM users WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        return bool(row["is_premium"]) if row else False

async def user_is_premium(tg_id: int) -> bool:
    # keep async-compatible wrapper
    return user_is_premium_sync(tg_id)

# -------------------- MENUS / NAV --------------------
async def send_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"Class {c}", callback_data=f"class|{c}")] for c in CLASSES]
    kb.append([InlineKeyboardButton("Buy Premium ⭐", callback_data="buy")])
    if update.effective_message:
        await update.effective_message.reply_text("📚 Choose your class:", reply_markup=InlineKeyboardMarkup(kb))

async def send_categories(query, class_num: str):
    rows = []
    for cat in CATEGORIES:
        rows.append([InlineKeyboardButton(cat, callback_data=f"cat|{class_num}|{cat}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await query.edit_message_text(f"Class {class_num} → Choose category:", reply_markup=InlineKeyboardMarkup(rows))

def list_subjects_sync(class_num: str, category: str) -> List[str]:
    with db() as con:
        cur = con.execute("SELECT DISTINCT subject FROM content WHERE class_num=? AND category=? ORDER BY subject", (class_num, category))
        return [r["subject"] for r in cur.fetchall() if r["subject"]]

async def send_subjects(query, class_num: str, category: str):
    subs = list_subjects_sync(class_num, category)
    if not subs:
        subs = ["Maths", "Physics", "Chemistry", "Biology", "English", "Hindi", "SST"]
    kb = []
    for s in subs:
        kb.append([InlineKeyboardButton(s, callback_data=f"sub|{class_num}|{category}|{s}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=f"class|{class_num}")])
    await query.edit_message_text(f"Class {class_num} → {category} → Choose subject:", reply_markup=InlineKeyboardMarkup(kb))

def list_chapters_sync(class_num: str, category: str, subject: str) -> List[str]:
    with db() as con:
        cur = con.execute("SELECT DISTINCT chapter FROM content WHERE class_num=? AND category=? AND subject=? ORDER BY chapter", (class_num, category, subject))
        return [r["chapter"] for r in cur.fetchall() if r["chapter"]]

async def send_chapters(query, class_num: str, category: str, subject: str):
    chs = list_chapters_sync(class_num, category, subject)
    if not chs:
        chs = ["Chapter 1", "Chapter 2", "Chapter 3"]
    kb = []
    for ch in chs:
        kb.append([InlineKeyboardButton(ch, callback_data=f"chap|{class_num}|{category}|{subject}|{ch}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=f"cat|{class_num}|{category}")])
    await query.edit_message_text(f"Class {class_num} → {category} → {subject} → Choose chapter:", reply_markup=InlineKeyboardMarkup(kb))

def fetch_items_sync(class_num: str, category: str, subject: str, chapter: str) -> List[sqlite3.Row]:
    with db() as con:
        cur = con.execute(
            "SELECT * FROM content WHERE class_num=? AND category=? AND subject=? AND chapter=? ORDER BY created_at DESC, id DESC",
            (class_num, category, subject, chapter),
        )
        return cur.fetchall()

async def send_items(query, tg_id: int, class_num: str, category: str, subject: str, chapter: str, page: int = 0):
    items = fetch_items_sync(class_num, category, subject, chapter)
    premium_user = await user_is_premium(tg_id)

    start = page * PAGE_SIZE
    page_items = items[start:start+PAGE_SIZE]

    text_lines = [f"Class {class_num} → {category} → {subject} → {chapter}", ""]
    if not page_items:
        text_lines.append("No items yet. Check again later ✨")
    else:
        for i, r in enumerate(page_items, 1):
            lock = "🔓" if (premium_user or r["premium"] == 0) else "🔒 Premium"
            text_lines.append(f"{i+start}. {r['title']} {lock}")
    text = "\n".join(text_lines)

    buttons = []
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page|{class_num}|{category}|{subject}|{chapter}|{page-1}"))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page|{class_num}|{category}|{subject}|{chapter}|{page+1}"))
    if nav:
        buttons.append(nav)
    if page_items:
        buttons.append([InlineKeyboardButton(f"📥 Send #1–#{len(page_items)}", callback_data=f"sendrange|{class_num}|{category}|{subject}|{chapter}|{start}|{len(page_items)}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"chap|{class_num}|{category}|{subject}|{chapter}")])
    if not premium_user:
        buttons.append([InlineKeyboardButton("⭐ Buy Premium", callback_data="buy")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def send_documents_by_range(message, tg_id: int, class_num: str, category: str, subject: str, chapter: str, start: int, count: int):
    items = fetch_items_sync(class_num, category, subject, chapter)
    premium_user = await user_is_premium(tg_id)
    subset = items[start:start+count]
    for r in subset:
        if r["premium"] and not premium_user:
            await message.reply_text(f"🔒 {r['title']} — Premium only. Use /buy to unlock.")
            continue
        try:
            await message.chat.send_action(action=ChatAction.UPLOAD_DOCUMENT)
            await message.reply_document(r["file_id"], caption=f"{r['title']}\n(Class {class_num} • {category} • {subject} • {chapter})")
        except Exception as e:
            await message.reply_text(f"Failed sending: {r['title']} — {e}")

# -------------------- COMMAND HANDLERS --------------------
PRICE_TEXT = (
    "\n".join([
        "⭐ *Premium Plans*",
        "• 1 Month: ₹99",
        "• 3 Months: ₹249",
        "• 12 Months: ₹699",
        "",
        "Premium me Test Series, Exclusive Handwritten Notes, Full Sample Papers, Fast Support unlock hoga.",
        "",
        "1) UPI se pay karein: `" + PAYMENT_UPI_ID + "`",
        "2) Payment note me likhein: `" + PAYMENT_NOTE + "`",
        "3) Yahan bhejein: /redeem <TXN_ID> (jaise /redeem 12345ABCD)",
        "4) Admin verify karega aur aapko Premium de diya jayega.",
    ])
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update, ctx)
    await update.message.reply_text("Namaste! 👋 Main Study Bot hoon — Notes, PYQ, Handwritten, Sample Papers & Quizzes.\nUse /menu to begin.")
    await send_menu(update, ctx)

async def menu_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update, ctx)
    await send_menu(update, ctx)

async def buy_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # if Razorpay available we'd show plans (callback via inline buttons)
    if RAZORPAY_AVAILABLE and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        kb = [[InlineKeyboardButton(PLANS[k]["label"], callback_data=f"rzp|{k}")] for k in PLANS.keys()]
        kb.append([InlineKeyboardButton("I already paid • Redeem", callback_data="buy")])
        await update.message.reply_text("Choose a Premium plan:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_markdown(PRICE_TEXT)

async def redeem_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update, ctx)
    parts = (update.message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /redeem <TXN_ID>")
        return
    txn = parts[1].strip()
    with db() as con:
        con.execute("INSERT INTO purchases (tg_id, txn_id, plan, created_at) VALUES (?,?,?,?)", (update.effective_user.id, txn, "manual-upi", datetime.utcnow().isoformat()))
        con.commit()
    await update.message.reply_text("Thanks! ✅ Aapka TXN ID receive ho gaya. Admin verify karte hi premium mil jayega.")
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(aid, f"Redeem request from {update.effective_user.full_name} (#{update.effective_user.id})\nTXN: {txn}")
        except Exception:
            pass

async def make_premium_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /make_premium <tg_id>")
        return
    target_id = int(parts[1])
    with db() as con:
        con.execute("UPDATE users SET is_premium=1 WHERE tg_id=?", (target_id,))
        con.commit()
    await update.message.reply_text(f"User {target_id} is now PREMIUM ✅")
    try:
        await ctx.bot.send_message(target_id, "Congrats! ⭐ Aapka premium activate ho gaya.")
    except Exception:
        pass

# -------------------- ADMIN UPLOAD --------------------
def parse_caption(caption: str) -> Optional[tuple]:
    # format: class|category|subject|chapter|title|premium
    try:
        parts = [p.strip() for p in caption.split("|")]
        if len(parts) != 6:
            return None
        class_num, category, subject, chapter, title, premium = parts
        if class_num not in CLASSES:
            return None
        if category not in CATEGORIES:
            return None
        prem = 1 if str(premium) in {"1", "true", "True", "yes", "Y"} else 0
        return class_num, category, subject, chapter, title, prem
    except Exception:
        return None

async def admin_doc_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    doc = update.message.document
    caption = update.message.caption or ""
    parsed = parse_caption(caption)
    if not doc or not parsed:
        await update.message.reply_text("Admin upload failed. Caption format:\nclass|category|subject|chapter|title|premium\nExample:\n10|PYQ|Maths|Ch-4 Trig|2019 Set-1|0")
        return
    class_num, category, subject, chapter, title, prem = parsed
    file_id = doc.file_id
    with db() as con:
        con.execute("INSERT INTO content (class_num, category, subject, chapter, title, file_id, premium, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (class_num, category, subject, chapter, title, file_id, prem, datetime.utcnow().isoformat()))
        con.commit()
    await update.message.reply_text(f"Saved ✅\nClass {class_num} • {category} • {subject} • {chapter}\nTitle: {title}\nPremium: {prem}")

# -------------------- QUIZZES --------------------
async def addquiz_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    # Usage: /addquiz 10 Maths Chapter-2 "Q?" | opt1 ; opt2 ; opt3 ; opt4 | correct_index | premium
    text = update.message.text or ""
    try:
        _, rest = text.split(" ", 1)
        class_num, rest = rest.split(" ", 1)
        subject, rest = rest.split(" ", 1)
        chapter, rest = rest.split(" ", 1)
        q, opts, corr, prem = [p.strip() for p in rest.split("|")]
        options = [o.strip() for o in opts.split(";")]
        if len(options) != 4:
            raise ValueError("Need 4 options")
        correct_index = int(corr)
        premium = 1 if prem in {"1", "true", "True", "yes", "Y"} else 0
    except Exception:
        await update.message.reply_text("Format: /addquiz <class> <subject> <chapter> <question> | opt1 ; opt2 ; opt3 ; opt4 | correct_index | premium\nExample:\n/addquiz 10 Maths Chapter-2 \"2+2?\" | 1 ; 2 ; 3 ; 4 | 4 | 0")
        return
    with db() as con:
        con.execute("""INSERT INTO quizzes (class_num, subject, chapter, question, option1, option2, option3, option4, correct_index, premium, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (class_num, subject, chapter, q.strip('"'), options[0], options[1], options[2], options[3], correct_index, premium, datetime.utcnow().isoformat()))
        con.commit()
    await update.message.reply_text("Quiz added ✅")

async def send_quiz_for_subject(update: Update, ctx: ContextTypes.DEFAULT_TYPE, class_num: str, subject: str, chapter: Optional[str]):
    premium_user = await user_is_premium(update.effective_user.id)
    with db() as con:
        if chapter:
            cur = con.execute("SELECT * FROM quizzes WHERE class_num=? AND subject=? AND chapter=? ORDER BY RANDOM() LIMIT 1", (class_num, subject, chapter))
        else:
            cur = con.execute("SELECT * FROM quizzes WHERE class_num=? AND subject=? ORDER BY RANDOM() LIMIT 1", (class_num, subject))
        row = cur.fetchone()
    if not row:
        await update.message.reply_text("No quiz available yet for this subject/chapter.")
        return
    if row["premium"] and not premium_user:
        await update.message.reply_text("🔒 This quiz is premium-only. Use /buy to unlock.")
        return
    await update.message.reply_poll(question=row["question"], options=[row["option1"], row["option2"], row["option3"], row["option4"]],
                                   type=Poll.QUIZ, correct_option_id=(row["correct_index"] - 1), is_anonymous=False)

async def quiz_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # /quiz 10 Maths [Chapter-2]
    parts = (update.message.text or "").split()
    if len(parts) < 3:
        await update.message.reply_text("Usage: /quiz <class> <subject> [chapter]")
        return
    class_num = parts[1]
    subject = parts[2]
    chapter = parts[3] if len(parts) >= 4 else None
    await send_quiz_for_subject(update, ctx, class_num, subject, chapter)

# -------------------- STATS / UTILS --------------------
async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    with db() as con:
        users = con.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        prem = con.execute("SELECT COUNT(*) as c FROM users WHERE is_premium=1").fetchone()["c"]
        docs = con.execute("SELECT COUNT(*) as c FROM content").fetchone()["c"]
        qz = con.execute("SELECT COUNT(*) as c FROM quizzes").fetchone()["c"]
    await update.message.reply_text(f"Users: {users}\nPremium: {prem}\nDocs: {docs}\nQuizzes: {qz}")

async def myid_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Welcome to Study Bot!\n\n"
        "/menu – Open class & categories\n"
        "/buy – Premium info\n"
        "/redeem <TXN_ID> – Submit payment txn\n"
        "/quiz <class> <subject> [chapter] – Take a quiz\n"
        "/myid – Show your Telegram ID\n\n"
        "Admin only:\n"
        "/make_premium <tg_id>\n"
        "/addquiz ... (see help)\n"
        "Send PDF with caption to upload: class|category|subject|chapter|title|premium\n"
    )
    await update.message.reply_text(help_text)

# -------------------- CALLBACKS (buttons) --------------------
async def buy_cb_router(query, ctx: ContextTypes.DEFAULT_TYPE, key: str):
    if not (RAZORPAY_AVAILABLE and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        await query.edit_message_text("Online checkout unavailable. Showing UPI instructions…")
        await ctx.bot.send_message(query.from_user.id, PRICE_TEXT, parse_mode="Markdown")
        return
    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    plan = PLANS.get(key)
    if not plan:
        await query.answer("Invalid plan", show_alert=True)
        return
    payload = {
        "amount": plan["amount"],
        "currency": "INR",
        "description": f"{PAYMENT_NOTE} — {plan['label']}",
        "cus