"""
Content Request Bot  (group-chat version)
-----------------------------------------
Each model has her own Telegram group chat (GC) with the bot in it.

Chatter DMs the bot: /request -> picks model -> picks type -> describes -> picks deadline -> Send.
The bot posts the request into THAT model's GC with buttons: Accept / Decline -> Mark done.
Model (or her manager) can reply to the request message in the GC with files or a note,
and the bot forwards it to the chatter's DMs.
Until it's marked done, the bot keeps reminding the GC (every REMIND_HOURS, and when overdue).

Env vars:
  BOT_TOKEN     token from @BotFather (required)
  ADMIN_IDS     comma-separated Telegram user IDs of admins/managers (required)
  DB_PATH       sqlite file (default: requests.db)
  TZ_NAME       timezone for displaying deadlines (default: Asia/Manila)
  REMIND_HOURS  hours between reminder pings for open requests (default: 24)
"""
import os
import html
import sqlite3
import secrets
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("content-bot")

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "requests.db")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Manila"))
REMIND_HOURS = float(os.environ.get("REMIND_HOURS", "24"))

TYPES = ["Sexting set", "Custom (fan-specific)", "PPV / mass message", "Pics",
         "Video", "Voice note", "Other"]
DEADLINES = [("2 hours", 2), ("Today (6h)", 6), ("24 hours", 24), ("48 hours", 48), ("Flexible", None)]
FMT = "%Y-%m-%d %H:%M:%S"
OPEN = ("sent", "accepted")

PICK_MODEL, PICK_TYPE, DETAILS, PICK_DEADLINE, CONFIRM = range(5)
CANCEL_ROW = [B("✖ Cancel", callback_data="cancel")]


# ---------------------------------------------------------------- database
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS chatters(tg_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS invites(token TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS models(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, chat_id INTEGER UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chatter_id INTEGER, model_id INTEGER, type TEXT, details TEXT,
            due_at TEXT, status TEXT DEFAULT 'sent', note TEXT, group_msg_id INTEGER,
            created_at TEXT, done_at TEXT, reminded_at TEXT, reminders INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS cards(chat_id INTEGER, msg_id INTEGER, req_id INTEGER,
            PRIMARY KEY(chat_id, msg_id));
        """)


def now():
    return datetime.now(timezone.utc)


def ts(d):
    return d.strftime(FMT)


def show_due(due):
    if not due:
        return "Flexible"
    d = datetime.strptime(due, FMT).replace(tzinfo=timezone.utc).astimezone(TZ)
    return d.strftime("%b %d, %I:%M %p")


def q1(sql, *args):
    with db() as c:
        return c.execute(sql, args).fetchone()


def qall(sql, *args):
    with db() as c:
        return c.execute(sql, args).fetchall()


def is_chatter(uid):
    return uid in ADMINS or q1("SELECT 1 FROM chatters WHERE tg_id=?", uid) is not None


def chatter_name(uid):
    r = q1("SELECT name FROM chatters WHERE tg_id=?", uid)
    return r["name"] if r else ("Admin" if uid in ADMINS else str(uid))


def model(mid):
    return q1("SELECT * FROM models WHERE id=?", mid)


def model_by_chat(chat_id):
    return q1("SELECT * FROM models WHERE chat_id=?", chat_id)


def get_req(rid):
    return q1("SELECT * FROM requests WHERE id=?", rid)


STATUS = {"sent": "🟡 Waiting for model", "accepted": "🔵 In progress",
          "done": "🟢 Done", "declined": "🔴 Declined"}


def req_text(r, header="Request"):
    e = html.escape
    m = model(r["model_id"])
    return (f"<b>{header} #{r['id']}</b>\n"
            f"Model: {e(m['name'] if m else '?')}\n"
            f"From chatter: {e(chatter_name(r['chatter_id']))}\n"
            f"Type: <b>{e(r['type'])}</b>\n"
            f"Due: {show_due(r['due_at'])}\n"
            f"Status: {STATUS.get(r['status'], r['status'])}\n\n{e(r['details'])}")


def buttons(r):
    if r["status"] == "sent":
        return M([[B("✅ Accept", callback_data=f"acc:{r['id']}"),
                   B("❌ Decline", callback_data=f"dec:{r['id']}")]])
    if r["status"] == "accepted":
        return M([[B("📦 Mark done", callback_data=f"done:{r['id']}")]])
    return None


async def safe_send(ctx, chat_id, text, **kw):
    try:
        return await ctx.bot.send_message(chat_id, text, parse_mode="HTML", **kw)
    except Exception as ex:
        log.warning("Could not message %s: %s", chat_id, ex)
        return None


async def post_to_gc(ctx, r, header):
    """Post a request card into the model's GC and remember the message id (for replies)."""
    m = model(r["model_id"])
    msg = await safe_send(ctx, m["chat_id"], req_text(r, header) +
                          "\n\n<i>Reply to this message with files or a note to send them to the chatter.</i>",
                          reply_markup=buttons(r))
    if msg:
        with db() as c:
            c.execute("UPDATE requests SET group_msg_id=? WHERE id=?", (msg.message_id, r["id"]))
            c.execute("INSERT OR REPLACE INTO cards VALUES(?,?,?)", (m["chat_id"], msg.message_id, r["id"]))
    return msg


# ---------------------------------------------------------------- start / admin
HELP_ADMIN = ("<b>Admin</b>\n"
              "/linkmodel Name – run this INSIDE a model's group chat to connect it\n"
              "/unlinkmodel – run inside the group to disconnect it\n"
              "/addchatter – one-time invite link for a chatter\n"
              "/team – list models & chatters\n/removechatter Name\n"
              "/all – every open request\n/request – make a request yourself")
HELP_CHATTER = ("<b>Chatter</b>\n/request – ask a model for content\n"
                "/mine – your open requests\n/cancel – abort a request in progress")
HELP_GROUP = ("<b>In this group</b>\n/todo – open requests for this model\n"
              "Tap the buttons on a request to accept / mark it done. Reply to a request to send files or a note to the chatter.")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if chat.type != "private":
        await update.message.reply_text(HELP_GROUP, parse_mode="HTML")
        return
    if ctx.args:
        token = ctx.args[0]
        with db() as c:
            if c.execute("DELETE FROM invites WHERE token=?", (token,)).rowcount:
                c.execute("INSERT OR REPLACE INTO chatters VALUES(?,?)", (user.id, user.full_name))
                for a in ADMINS:
                    await safe_send(ctx, a, f"✅ {html.escape(user.full_name)} joined as a chatter.")
            else:
                await update.message.reply_text("That invite link is invalid or already used.")
    if user.id in ADMINS:
        await update.message.reply_text(HELP_ADMIN + "\n\n" + HELP_CHATTER, parse_mode="HTML")
    elif is_chatter(user.id):
        await update.message.reply_text(f"Hi {html.escape(user.first_name)} 👋\n\n" + HELP_CHATTER, parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"You're not registered yet. Ask your manager for an invite link.\nYour Telegram ID: {user.id}")


def admin_only(fn):
    async def wrap(update: Update, ctx):
        if update.effective_user.id not in ADMINS:
            await update.message.reply_text("Admins only.")
            return
        return await fn(update, ctx)
    return wrap


@admin_only
async def linkmodel(update, ctx):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Run /linkmodel Name inside the model's group chat (add me to the group first).")
        return
    name = " ".join(ctx.args).strip() or chat.title
    with db() as c:
        c.execute("DELETE FROM models WHERE chat_id=? OR name=?", (chat.id, name))
        c.execute("INSERT INTO models(name, chat_id) VALUES(?,?)", (name, chat.id))
    await update.message.reply_text(
        f"✅ This group is now <b>{html.escape(name)}</b>'s request channel. "
        "Chatters' requests for her will show up here.", parse_mode="HTML")


@admin_only
async def unlinkmodel(update, ctx):
    with db() as c:
        n = c.execute("DELETE FROM models WHERE chat_id=?", (update.effective_chat.id,)).rowcount
    await update.message.reply_text("Disconnected." if n else "This group wasn't linked.")


@admin_only
async def addchatter(update, ctx):
    token = secrets.token_urlsafe(8)
    with db() as c:
        c.execute("INSERT INTO invites VALUES(?)", (token,))
    await update.message.reply_text(
        f"Send this one-time link to a chatter:\nhttps://t.me/{ctx.bot.username}?start={token}")


@admin_only
async def team(update, ctx):
    ms = qall("SELECT name FROM models ORDER BY name")
    cs = qall("SELECT name FROM chatters ORDER BY name")
    await update.message.reply_text(
        "Models: " + (", ".join(m["name"] for m in ms) or "none") +
        "\nChatters: " + (", ".join(c["name"] for c in cs) or "none"))


@admin_only
async def removechatter(update, ctx):
    name = " ".join(ctx.args)
    with db() as c:
        n = c.execute("DELETE FROM chatters WHERE name=?", (name,)).rowcount
    await update.message.reply_text(f"Removed {n} chatter(s) named '{name}'.")


@admin_only
async def all_open(update, ctx):
    rows = qall("SELECT * FROM requests WHERE status IN ('sent','accepted') ORDER BY id")
    if not rows:
        await update.message.reply_text("No open requests 🎉")
    for r in rows[:30]:
        await update.message.reply_text(req_text(r), parse_mode="HTML")


# ---------------------------------------------------------------- chatter: /request flow (in DMs)
async def req_start(update: Update, ctx):
    if update.effective_chat.type != "private":
        await update.message.reply_text("DM me to make a request 🙂")
        return ConversationHandler.END
    if not is_chatter(update.effective_user.id):
        await update.message.reply_text("You're not registered as a chatter.")
        return ConversationHandler.END
    models = qall("SELECT * FROM models ORDER BY name")
    if not models:
        await update.message.reply_text("No model group chats are linked yet.")
        return ConversationHandler.END
    kb = [[B(m["name"], callback_data=f"m:{m['id']}")] for m in models] + [CANCEL_ROW]
    await update.message.reply_text("Which model do you need content from?", reply_markup=M(kb))
    return PICK_MODEL


async def _cancelled(q):
    await q.edit_message_text("Cancelled.")
    return ConversationHandler.END


async def pick_model(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await _cancelled(q)
    ctx.user_data["req"] = {"model": int(q.data[2:])}
    kb = [[B(t, callback_data=f"t:{i}")] for i, t in enumerate(TYPES)] + [CANCEL_ROW]
    await q.edit_message_text(f"What do you need from {html.escape(model(int(q.data[2:]))['name'])}?",
                              reply_markup=M(kb))
    return PICK_TYPE


async def pick_type(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await _cancelled(q)
    ctx.user_data["req"]["type"] = TYPES[int(q.data[2:])]
    await q.edit_message_text(
        f"<b>{html.escape(ctx.user_data['req']['type'])}</b>\n\nDescribe exactly what you need "
        "(outfit, how many pics/how long, angles, fan's name & requests, price agreed…).", parse_mode="HTML")
    return DETAILS


async def got_details(update: Update, ctx):
    ctx.user_data["req"]["details"] = update.message.text
    kb = [[B(lbl, callback_data=f"d:{i}")] for i, (lbl, _) in enumerate(DEADLINES)] + [CANCEL_ROW]
    await update.message.reply_text("When do you need it by?", reply_markup=M(kb))
    return PICK_DEADLINE


async def pick_deadline(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await _cancelled(q)
    hours = DEADLINES[int(q.data[2:])][1]
    r = ctx.user_data["req"]
    r["due_at"] = ts(now() + timedelta(hours=hours)) if hours else None
    e = html.escape
    summary = (f"<b>Check your request</b>\nModel: {e(model(r['model'])['name'])}\nType: {e(r['type'])}\n"
               f"Due: {show_due(r['due_at'])}\n\n{e(r['details'])}")
    kb = [[B("📤 Send to model's GC", callback_data="send")], CANCEL_ROW]
    await q.edit_message_text(summary, parse_mode="HTML", reply_markup=M(kb))
    return CONFIRM


async def confirm(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        return await _cancelled(q)
    r = ctx.user_data.pop("req")
    with db() as c:
        rid = c.execute(
            "INSERT INTO requests(chatter_id, model_id, type, details, due_at, created_at) VALUES(?,?,?,?,?,?)",
            (update.effective_user.id, r["model"], r["type"], r["details"], r["due_at"], ts(now()))).lastrowid
    name = html.escape(model(r["model"])["name"])
    if await post_to_gc(ctx, get_req(rid), "📩 New request"):
        await q.edit_message_text(f"Sent ✅ Request #{rid} is in {name}'s GC. "
                                  "I'll DM you when it's accepted and done, and I'll keep reminding her until then.")
    else:
        await q.edit_message_text(f"Request #{rid} saved, but I couldn't post in {name}'s GC "
                                  "(was I removed from the group?). Admins have been notified.")
        for a in ADMINS:
            await safe_send(ctx, a, f"⚠️ Couldn't post request #{rid} to {name}'s group chat.")
    return ConversationHandler.END


async def cancel(update: Update, ctx):
    ctx.user_data.pop("req", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def mine(update: Update, ctx):
    rows = qall("SELECT * FROM requests WHERE chatter_id=? AND status IN ('sent','accepted') ORDER BY id",
                update.effective_user.id)
    if not rows:
        await update.message.reply_text("You have no open requests.")
    for r in rows[:30]:
        await update.message.reply_text(req_text(r), parse_mode="HTML")


# ---------------------------------------------------------------- model side (in the GC)
async def gc_action(update: Update, ctx):
    q = update.callback_query
    action, rid = q.data.split(":")
    rid = int(rid)
    r = get_req(rid)
    m = model(r["model_id"]) if r else None
    if not r or not m or m["chat_id"] != q.message.chat.id:
        await q.answer("Request not found.", show_alert=True)
        return
    who = html.escape(update.effective_user.first_name)
    new = {"acc": ("sent", "accepted"), "dec": ("sent", "declined"), "done": ("accepted", "done")}[action]
    if r["status"] != new[0]:
        await q.answer("Already updated.")
        return
    await q.answer()
    with db() as c:
        c.execute("UPDATE requests SET status=?, done_at=? WHERE id=?",
                  (new[1], ts(now()) if new[1] == "done" else None, rid))
    r = get_req(rid)
    await q.edit_message_text(req_text(r) + f"\n\n<i>Updated by {who}</i>", parse_mode="HTML",
                              reply_markup=buttons(r))
    msgs = {"accepted": f"👍 {html.escape(m['name'])} accepted request #{rid} ({html.escape(r['type'])}).",
            "declined": f"❌ {html.escape(m['name'])} declined request #{rid}. Check with her or your manager.",
            "done": f"🎉 {html.escape(m['name'])} finished request #{rid} ({html.escape(r['type'])})!"}
    await safe_send(ctx, r["chatter_id"], msgs[r["status"]])


async def gc_reply(update: Update, ctx):
    """Model replies to a request card in the GC with files or a note -> forward to the chatter."""
    msg = update.message
    rt = msg.reply_to_message
    if not rt or not rt.from_user or rt.from_user.id != ctx.bot.id:
        return
    r = q1("SELECT r.* FROM cards k JOIN requests r ON r.id=k.req_id WHERE k.chat_id=? AND k.msg_id=?",
           msg.chat.id, rt.message_id)
    if not r:
        return
    try:
        await ctx.bot.send_message(r["chatter_id"], f"📎 From {model(r['model_id'])['name']} for request #{r['id']}:")
        await msg.copy(r["chatter_id"])
        await msg.reply_text("Sent to the chatter ✅ Tap “Mark done” on the request when it's complete.")
    except Exception as ex:
        log.warning("forward failed: %s", ex)
        await msg.reply_text("Couldn't reach the chatter (they may have blocked the bot).")


async def todo(update: Update, ctx):
    m = model_by_chat(update.effective_chat.id)
    if not m:
        await update.message.reply_text("This group isn't linked to a model. Admin: /linkmodel Name")
        return
    rows = qall("SELECT * FROM requests WHERE model_id=? AND status IN ('sent','accepted') "
                "ORDER BY due_at IS NULL, due_at", m["id"])
    if not rows:
        await update.message.reply_text("Nothing open – all caught up ✨")
    for r in rows[:30]:
        await post_to_gc(ctx, r, "📋 Open request")


# ---------------------------------------------------------------- reminders (notifier)
async def reminders(ctx: ContextTypes.DEFAULT_TYPE):
    n = now()
    cutoff = ts(n - timedelta(hours=REMIND_HOURS))
    rows = qall("""SELECT * FROM requests WHERE status IN ('sent','accepted')
                   AND COALESCE(reminded_at, created_at) < ?
                   OR (status IN ('sent','accepted') AND due_at < ? AND reminded_at IS NULL)""",
                cutoff, ts(n))
    for r in rows:
        overdue = r["due_at"] and r["due_at"] < ts(n)
        if overdue:
            head = "🚨 OVERDUE – please send this ASAP"
        elif r["status"] == "sent":
            head = "⏰ Reminder – still waiting on you to accept"
        else:
            head = "⏰ Reminder – this is still not done"
        await post_to_gc(ctx, r, head)
        if overdue and not r["reminders"]:
            await safe_send(ctx, r["chatter_id"], f"⏰ Request #{r['id']} passed its deadline – I've reminded the model.")
        with db() as c:
            c.execute("UPDATE requests SET reminded_at=?, reminders=reminders+1 WHERE id=?", (ts(n), r["id"]))
        if r["reminders"] + 1 == 3:  # escalate to admins after 3 nudges
            for a in ADMINS:
                await safe_send(ctx, a, "⚠️ Reminded 3× with no completion:\n\n" + req_text(r))


# ---------------------------------------------------------------- main
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("request", req_start)],
        states={
            PICK_MODEL: [CallbackQueryHandler(pick_model, pattern=r"^(m:|cancel)")],
            PICK_TYPE: [CallbackQueryHandler(pick_type, pattern=r"^(t:|cancel)")],
            DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, got_details)],
            PICK_DEADLINE: [CallbackQueryHandler(pick_deadline, pattern=r"^(d:|cancel)")],
            CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^(send|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    for cmd, fn in [("start", start), ("help", start), ("linkmodel", linkmodel), ("unlinkmodel", unlinkmodel),
                    ("addchatter", addchatter), ("team", team), ("removechatter", removechatter),
                    ("all", all_open), ("mine", mine), ("todo", todo)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(gc_action, pattern=r"^(acc|dec|done):\d+$"))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.REPLY & ~filters.COMMAND, gc_reply), group=1)

    app.job_queue.run_repeating(reminders, interval=900, first=30)  # checks every 15 min
    log.info("Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()
