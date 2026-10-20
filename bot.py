import logging
import os
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Dubai"))
DB_PATH = Path(os.getenv("DATABASE_PATH", "lucky_draw.sqlite3"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS draws (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            winner_count INTEGER NOT NULL,
            closes_at TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open'
        );
        CREATE TABLE IF NOT EXISTS entries (
            draw_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            display_name TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (draw_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            welcome_enabled INTEGER NOT NULL DEFAULT 1,
            welcome_text TEXT NOT NULL DEFAULT '🎉 欢迎 {name} 加入群组！'
        );
        CREATE TABLE IF NOT EXISTS replies (
            chat_id INTEGER NOT NULL, keyword TEXT NOT NULL, response TEXT NOT NULL,
            PRIMARY KEY (chat_id, keyword)
        );
        CREATE TABLE IF NOT EXISTS banned_words (
            chat_id INTEGER NOT NULL, word TEXT NOT NULL,
            PRIMARY KEY (chat_id, word)
        );
        """)
        conn.commit()


async def is_admin(update: Update, user_id: int) -> bool:
    member = await update.effective_chat.get_member(user_id)
    return member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


def ensure_settings(chat_id: int):
    with closing(db()) as conn:
        conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES (?)", (chat_id,))
        conn.commit()


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 群抽奖", callback_data="menu:draw"), InlineKeyboardButton("👋 入群欢迎", callback_data="menu:welcome")],
        [InlineKeyboardButton("💬 自动回复", callback_data="menu:reply"), InlineKeyboardButton("🚫 违禁词", callback_data="menu:words")],
        [InlineKeyboardButton("📊 群组统计", callback_data="menu:stats"), InlineKeyboardButton("📖 使用帮助", callback_data="menu:help")],
    ])


def draw_text(draw, participant_count=0):
    closing = datetime.fromisoformat(draw["closes_at"]).astimezone(TIMEZONE)
    return (
        "🎰 <b>盛世豪莱尔幸运抽奖</b>\n\n"
        f"🎁 <b>奖品：</b>{draw['prize']}\n"
        f"🏆 <b>获奖人数：</b>{draw['winner_count']} 位\n"
        f"⏰ <b>开奖时间：</b>{closing:%Y-%m-%d %H:%M}（迪拜时间）\n"
        f"👥 <b>已参加：</b>{participant_count} 人\n\n"
        "点击下方按钮即可参与。每人仅限一次。"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        ensure_settings(update.effective_chat.id)
    await update.message.reply_text(
        "🎰 <b>盛世豪莱尔群管机器人</b>\n\n支持群抽奖、入群欢迎、自动回复、违禁词管理、禁言和群组统计。",
        parse_mode="HTML", reply_markup=main_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>盛世豪莱尔抽奖机器人</b>\n\n"
        "管理员创建抽奖：\n"
        "<code>/newdraw 奖品 | 获奖人数 | YYYY-MM-DD HH:MM</code>\n\n"
        "示例：\n"
        "<code>/newdraw 100 USDT 奖励 | 3 | 2026-10-21 20:00</code>\n\n"
        "重抽：<code>/redraw 抽奖ID</code>\n\n"
        "<b>群管理（仅管理员）</b>\n"
        "<code>/welcome on</code> 或 <code>/welcome off</code>\n"
        "<code>/setwelcome 欢迎文案</code>（可使用 {name}）\n"
        "<code>/reply add 关键词 | 回复内容</code>\n"
        "<code>/reply del 关键词</code>\n"
        "<code>/banword add 违禁词</code>\n"
        "<code>/banword del 违禁词</code>\n"
        "回复成员消息后发送 <code>/mute 10m</code> 可禁言。\n"
        "<code>/stats</code> 查看群组统计。\n\n"
        "时间按迪拜时间（UTC+4）填写。",
        parse_mode="HTML",
    )


async def new_draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("请在需要抽奖的群组内使用此命令。")
        return
    if not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("只有群管理员可以创建抽奖。")
        return
    raw = update.message.text.partition(" ")[2].strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 3 or not parts[0]:
        await help_command(update, context)
        return
    try:
        winner_count = int(parts[1])
        closes_at = datetime.strptime(parts[2], "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
        if winner_count < 1 or closes_at <= datetime.now(TIMEZONE):
            raise ValueError
    except ValueError:
        await update.message.reply_text("格式或时间不正确。获奖人数须大于 0，且开奖时间必须在未来。")
        return
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO draws(chat_id, prize, winner_count, closes_at, created_by) VALUES (?, ?, ?, ?, ?)",
            (update.effective_chat.id, parts[0], winner_count, closes_at.isoformat(), update.effective_user.id),
        )
        draw_id = cur.lastrowid
        conn.commit()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎉 立即参加抽奖", callback_data=f"join:{draw_id}")]])
    with closing(db()) as conn:
        draw = conn.execute("SELECT * FROM draws WHERE id = ?", (draw_id,)).fetchone()
    message = await update.message.reply_text(draw_text(draw), parse_mode="HTML", reply_markup=keyboard)
    with closing(db()) as conn:
        conn.execute("UPDATE draws SET message_id = ? WHERE id = ?", (message.message_id, draw_id))
        conn.commit()
    context.job_queue.run_once(close_draw, closes_at, data={"draw_id": draw_id}, name=f"draw:{draw_id}")


async def join_draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draw_id = int(query.data.split(":", 1)[1])
    user = query.from_user
    with closing(db()) as conn:
        draw = conn.execute("SELECT * FROM draws WHERE id = ?", (draw_id,)).fetchone()
        if not draw or draw["status"] != "open":
            await query.answer("该抽奖已结束。", show_alert=True)
            return
        try:
            conn.execute(
                "INSERT INTO entries(draw_id, user_id, username, display_name, joined_at) VALUES (?, ?, ?, ?, ?)",
                (draw_id, user.id, user.username, user.full_name, datetime.now(TIMEZONE).isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            await query.answer("你已经参加过本次抽奖。", show_alert=True)
            return
        count = conn.execute("SELECT COUNT(*) FROM entries WHERE draw_id = ?", (draw_id,)).fetchone()[0]
    await query.answer("参加成功，祝你好运！", show_alert=True)
    await query.edit_message_text(draw_text(draw, count), parse_mode="HTML", reply_markup=query.message.reply_markup)


def participant_label(row):
    return f"@{row['username']}" if row["username"] else row["display_name"]


async def close_draw(context: ContextTypes.DEFAULT_TYPE):
    draw_id = context.job.data["draw_id"]
    with closing(db()) as conn:
        draw = conn.execute("SELECT * FROM draws WHERE id = ?", (draw_id,)).fetchone()
        if not draw or draw["status"] != "open":
            return
        entries = conn.execute("SELECT * FROM entries WHERE draw_id = ?", (draw_id,)).fetchall()
        conn.execute("UPDATE draws SET status = 'closed' WHERE id = ?", (draw_id,))
        conn.commit()
    if not entries:
        await context.bot.send_message(draw["chat_id"], f"🎰 抽奖 #{draw_id} 已结束。\n\n奖品：{draw['prize']}\n本次无人参加，未产生获奖者。")
        return
    winners = random.sample(entries, min(draw["winner_count"], len(entries)))
    names = "\n".join(f"{i + 1}. {participant_label(row)}" for i, row in enumerate(winners))
    await context.bot.send_message(
        draw["chat_id"],
        f"🎉 <b>盛世豪莱尔抽奖结果</b>\n\n🎁 奖品：{draw['prize']}\n🏆 获奖者：\n{names}\n\n请获奖者联系群管理员领取奖励。",
        parse_mode="HTML",
    )


async def redraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private" or not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("只有本群管理员可以重抽。")
        return
    raw = update.message.text.partition(" ")[2].strip()
    if not raw.isdigit():
        await update.message.reply_text("用法：/redraw 抽奖ID")
        return
    with closing(db()) as conn:
        draw = conn.execute("SELECT * FROM draws WHERE id = ? AND chat_id = ?", (int(raw), update.effective_chat.id)).fetchone()
        entries = conn.execute("SELECT * FROM entries WHERE draw_id = ?", (int(raw),)).fetchall()
    if not draw or not entries:
        await update.message.reply_text("找不到该抽奖，或该抽奖没有参与者。")
        return
    winners = random.sample(entries, min(draw["winner_count"], len(entries)))
    names = "\n".join(f"{i + 1}. {participant_label(row)}" for i, row in enumerate(winners))
    await update.message.reply_text(f"🔄 <b>抽奖 #{raw} 重抽结果</b>\n\n{names}", parse_mode="HTML")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    pages = {
        "draw": "🎁 <b>群抽奖</b>\n<code>/newdraw 奖品 | 获奖人数 | YYYY-MM-DD HH:MM</code>",
        "welcome": "👋 <b>入群欢迎</b>\n<code>/welcome on</code> 或 <code>/welcome off</code>\n<code>/setwelcome 欢迎 {name}</code>",
        "reply": "💬 <b>自动回复</b>\n<code>/reply add 关键词 | 回复内容</code>\n<code>/reply del 关键词</code>",
        "words": "🚫 <b>违禁词</b>\n<code>/banword add 违禁词</code>\n<code>/banword del 违禁词</code>",
        "stats": "📊 <b>群组统计</b>\n在群内发送 <code>/stats</code>。",
        "help": "📖 在群内发送 /help 查看全部命令。",
    }
    await q.message.reply_text(pages[key], parse_mode="HTML")


async def welcome_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private" or not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("仅群管理员可设置欢迎消息。")
        return
    raw = update.message.text.partition(" ")[2].strip().lower()
    if raw not in {"on", "off"}:
        await update.message.reply_text("用法：/welcome on 或 /welcome off")
        return
    ensure_settings(update.effective_chat.id)
    with closing(db()) as conn:
        conn.execute("UPDATE settings SET welcome_enabled = ? WHERE chat_id = ?", (raw == "on", update.effective_chat.id))
        conn.commit()
    await update.message.reply_text(f"入群欢迎已{'开启' if raw == 'on' else '关闭'}。")


async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private" or not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("仅群管理员可设置欢迎文案。")
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("用法：/setwelcome 欢迎 {name}")
        return
    ensure_settings(update.effective_chat.id)
    with closing(db()) as conn:
        conn.execute("UPDATE settings SET welcome_text = ? WHERE chat_id = ?", (text, update.effective_chat.id))
        conn.commit()
    await update.message.reply_text("欢迎文案已保存。")


async def reply_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private" or not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("仅群管理员可设置自动回复。")
        return
    raw = update.message.text.partition(" ")[2].strip()
    action, _, body = raw.partition(" ")
    if action == "add" and "|" in body:
        keyword, response = [x.strip() for x in body.split("|", 1)]
        if keyword and response:
            with closing(db()) as conn:
                conn.execute("INSERT OR REPLACE INTO replies(chat_id, keyword, response) VALUES (?, ?, ?)", (update.effective_chat.id, keyword.lower(), response))
                conn.commit()
            await update.message.reply_text("自动回复已添加。")
            return
    if action == "del" and body.strip():
        with closing(db()) as conn:
            conn.execute("DELETE FROM replies WHERE chat_id = ? AND keyword = ?", (update.effective_chat.id, body.strip().lower()))
            conn.commit()
        await update.message.reply_text("自动回复已删除。")
        return
    await update.message.reply_text("用法：/reply add 关键词 | 回复内容\n或 /reply del 关键词")


async def banword_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private" or not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("仅群管理员可设置违禁词。")
        return
    raw = update.message.text.partition(" ")[2].strip()
    action, _, word = raw.partition(" ")
    word = word.strip().lower()
    if action in {"add", "del"} and word:
        with closing(db()) as conn:
            if action == "add": conn.execute("INSERT OR IGNORE INTO banned_words(chat_id, word) VALUES (?, ?)", (update.effective_chat.id, word))
            else: conn.execute("DELETE FROM banned_words WHERE chat_id = ? AND word = ?", (update.effective_chat.id, word))
            conn.commit()
        await update.message.reply_text(f"违禁词已{'添加' if action == 'add' else '删除'}。")
        return
    await update.message.reply_text("用法：/banword add 违禁词\n或 /banword del 违禁词")


async def mute_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private" or not await is_admin(update, update.effective_user.id):
        await update.message.reply_text("仅群管理员可禁言成员。")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复成员消息后发送 /mute 10m。")
        return
    raw = update.message.text.partition(" ")[2].strip().lower()
    try:
        amount, unit = int(raw[:-1]), raw[-1]
        seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
    except (ValueError, KeyError):
        await update.message.reply_text("时间格式：10m、2h 或 1d。")
        return
    target = update.message.reply_to_message.from_user
    await update.effective_chat.restrict_member(target.id, ChatPermissions(can_send_messages=False), until_date=datetime.now(TIMEZONE) + timedelta(seconds=seconds))
    await update.message.reply_text(f"已禁言 {target.full_name}。")


async def group_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("请在群内使用 /stats。")
        return
    with closing(db()) as conn:
        draws = conn.execute("SELECT COUNT(*) FROM draws WHERE chat_id = ?", (update.effective_chat.id,)).fetchone()[0]
        entries = conn.execute("SELECT COUNT(*) FROM entries e JOIN draws d ON e.draw_id = d.id WHERE d.chat_id = ?", (update.effective_chat.id,)).fetchone()[0]
    members = await update.effective_chat.get_member_count()
    await update.message.reply_text(f"📊 <b>群组统计</b>\n\n👥 当前成员：{members}\n🎰 创建抽奖：{draws} 场\n🎉 累计参与：{entries} 人次", parse_mode="HTML")


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_settings(update.effective_chat.id)
    with closing(db()) as conn:
        settings = conn.execute("SELECT * FROM settings WHERE chat_id = ?", (update.effective_chat.id,)).fetchone()
    if settings and settings["welcome_enabled"]:
        for user in update.message.new_chat_members:
            if not user.is_bot:
                await update.message.reply_text(settings["welcome_text"].replace("{name}", user.full_name))


async def moderate_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private" or not update.message.text: return
    text = update.message.text.lower()
    with closing(db()) as conn:
        words = conn.execute("SELECT word FROM banned_words WHERE chat_id = ?", (update.effective_chat.id,)).fetchall()
        reply = conn.execute("SELECT response FROM replies WHERE chat_id = ? AND keyword = ?", (update.effective_chat.id, text.strip())).fetchone()
    if any(row["word"] in text for row in words):
        try: await update.message.delete()
        except Exception: logger.warning("No permission to delete forbidden-word message")
    elif reply:
        await update.message.reply_text(reply["response"])


async def restore_jobs(app: Application):
    now = datetime.now(TIMEZONE)
    with closing(db()) as conn:
        draws = conn.execute("SELECT * FROM draws WHERE status = 'open'").fetchall()
    for draw in draws:
        when = datetime.fromisoformat(draw["closes_at"])
        if when <= now:
            app.job_queue.run_once(close_draw, 1, data={"draw_id": draw["id"]}, name=f"draw:{draw['id']}")
        else:
            app.job_queue.run_once(close_draw, when, data={"draw_id": draw["id"]}, name=f"draw:{draw['id']}")


def main():
    if not TOKEN:
        raise RuntimeError("请在环境变量 BOT_TOKEN 中填写 Telegram Bot Token。")
    init_db()
    app = Application.builder().token(TOKEN).post_init(restore_jobs).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("newdraw", new_draw))
    app.add_handler(CommandHandler("redraw", redraw))
    app.add_handler(CommandHandler("welcome", welcome_setting))
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("reply", reply_setting))
    app.add_handler(CommandHandler("banword", banword_setting))
    app.add_handler(CommandHandler("mute", mute_member))
    app.add_handler(CommandHandler("stats", group_stats))
    app.add_handler(CallbackQueryHandler(join_draw, pattern=r"^join:\d+$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, moderate_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
