import asyncio
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(TIMEZONE)


def init_db():
    with closing(db()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS draws (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            prize TEXT NOT NULL, winner_count INTEGER NOT NULL, closes_at TEXT NOT NULL,
            created_by INTEGER NOT NULL, message_id INTEGER, status TEXT NOT NULL DEFAULT 'open',
            min_points INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS entries (
            draw_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT,
            display_name TEXT NOT NULL, joined_at TEXT NOT NULL,
            PRIMARY KEY (draw_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY, welcome_enabled INTEGER NOT NULL DEFAULT 1,
            welcome_text TEXT NOT NULL DEFAULT '🎉 欢迎 {name} 加入群组！',
            points_per_message INTEGER NOT NULL DEFAULT 1,
            points_interval_seconds INTEGER NOT NULL DEFAULT 60
        );
        CREATE TABLE IF NOT EXISTS replies (
            chat_id INTEGER NOT NULL, keyword TEXT NOT NULL, response TEXT NOT NULL,
            PRIMARY KEY (chat_id, keyword)
        );
        CREATE TABLE IF NOT EXISTS banned_words (
            chat_id INTEGER NOT NULL, word TEXT NOT NULL,
            PRIMARY KEY (chat_id, word)
        );
        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT,
            display_name TEXT NOT NULL, total_points INTEGER NOT NULL DEFAULT 0,
            last_point_at TEXT, last_seen_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS point_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            points INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_checkins (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, checkin_date TEXT NOT NULL,
            checked_at TEXT NOT NULL, PRIMARY KEY (chat_id, user_id, checkin_date)
        );
        """)
        draw_columns = {r["name"] for r in conn.execute("PRAGMA table_info(draws)")}
        if "min_points" not in draw_columns:
            conn.execute("ALTER TABLE draws ADD COLUMN min_points INTEGER NOT NULL DEFAULT 0")
        setting_columns = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
        for name, definition in {"points_per_message": "INTEGER NOT NULL DEFAULT 1", "points_interval_seconds": "INTEGER NOT NULL DEFAULT 60", "checkin_points": "INTEGER NOT NULL DEFAULT 1"}.items():
            if name not in setting_columns:
                conn.execute(f"ALTER TABLE settings ADD COLUMN {name} {definition}")
        conn.commit()


def ensure_settings(chat_id):
    with closing(db()) as conn:
        conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES (?)", (chat_id,))
        conn.commit()


async def is_admin(chat, user_id):
    member = await chat.get_member(user_id)
    return member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 每日签到", callback_data="menu:checkin"), InlineKeyboardButton("👤 我的积分", callback_data="menu:me")],
        [InlineKeyboardButton("🏆 积分排行榜", callback_data="menu:rank")],
        [InlineKeyboardButton("🎁 当前抽奖", callback_data="menu:draw"), InlineKeyboardButton("📜 抽奖记录", callback_data="menu:history")],
        [InlineKeyboardButton("⚙️ 管理员面板", callback_data="menu:admin"), InlineKeyboardButton("📖 使用帮助", callback_data="menu:help")],
    ])


def rank_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("本周榜", callback_data="rank:week"), InlineKeyboardButton("本月榜", callback_data="rank:month")],
        [InlineKeyboardButton("总积分榜", callback_data="rank:total")],
    ])


def draw_keyboard(draw_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎉 立即参加抽奖", callback_data=f"join:{draw_id}")]])


def label(row):
    return f"@{row['username']}" if row["username"] else row["display_name"]


def draw_text(draw, count=0):
    closes = datetime.fromisoformat(draw["closes_at"]).astimezone(TIMEZONE)
    condition = "全体成员均可参加" if not draw["min_points"] else f"至少 {draw['min_points']} 积分"
    return ("🎰 <b>盛世豪莱尔幸运抽奖</b>\n\n"
            f"🎁 <b>奖品：</b>{draw['prize']}\n🏆 <b>获奖人数：</b>{draw['winner_count']} 位\n"
            f"⭐ <b>参与条件：</b>{condition}\n⏰ <b>开奖时间：</b>{closes:%Y-%m-%d %H:%M}（迪拜时间）\n"
            f"👥 <b>已参加：</b>{count} 人\n\n点击下方按钮即可参与。每人仅限一次。")


def period_start(period):
    current = now()
    if period == "week":
        return (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def ranking(chat_id, period="week", limit=10):
    start = period_start(period)
    with closing(db()) as conn:
        if start:
            return conn.execute("""SELECT m.user_id,m.username,m.display_name,COALESCE(SUM(e.points),0) AS points
                FROM members m JOIN point_events e ON e.chat_id=m.chat_id AND e.user_id=m.user_id
                WHERE m.chat_id=? AND e.created_at>=? GROUP BY m.user_id
                ORDER BY points DESC,m.display_name COLLATE NOCASE LIMIT ?""", (chat_id, start.isoformat(), limit)).fetchall()
        return conn.execute("SELECT user_id,username,display_name,total_points AS points FROM members WHERE chat_id=? ORDER BY total_points DESC,display_name COLLATE NOCASE LIMIT ?", (chat_id, limit)).fetchall()


def ranking_text(chat_id, period="week"):
    titles = {"week": "本周活跃积分榜", "month": "本月活跃积分榜", "total": "总积分榜"}
    rows = ranking(chat_id, period)
    if not rows:
        return f"🏆 <b>{titles[period]}</b>\n\n暂时还没有积分记录。"
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'{i+1}.'} {label(row)} — <b>{row['points']}</b> 分" for i, row in enumerate(rows)]
    return f"🏆 <b>{titles[period]}</b>\n\n" + "\n".join(lines) + "\n\n正常发言即可累计积分。"


def upsert_member(chat_id, user):
    if user.is_bot:
        return
    with closing(db()) as conn:
        conn.execute("""INSERT INTO members(chat_id,user_id,username,display_name,last_seen_at) VALUES (?,?,?,?,?)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,last_seen_at=excluded.last_seen_at""", (chat_id, user.id, user.username, user.full_name, now().isoformat()))
        conn.commit()


def member_points(chat_id, user_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT total_points FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    return row[0] if row else 0


def add_points(chat_id, user, points, reason):
    upsert_member(chat_id, user)
    with closing(db()) as conn:
        conn.execute("UPDATE members SET total_points=MAX(0,total_points+?) WHERE chat_id=? AND user_id=?", (points, chat_id, user.id))
        conn.execute("INSERT INTO point_events(chat_id,user_id,points,reason,created_at) VALUES (?,?,?,?,?)", (chat_id, user.id, points, reason, now().isoformat()))
        total = conn.execute("SELECT total_points FROM members WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()[0]
        conn.commit()
    return total


async def start(update, context):
    if update.effective_chat.type != "private":
        ensure_settings(update.effective_chat.id)
    await update.message.reply_text("🎰 <b>盛世豪莱尔积分抽奖机器人 V2</b>\n\n发言累计积分，积分参与抽奖，榜单激励群活跃。", parse_mode="HTML", reply_markup=main_menu())


async def help_command(update, context):
    await update.message.reply_text("""<b>盛世豪莱尔机器人 V2 使用说明</b>

<b>成员功能</b>
<code>/checkin</code> 每日签到；<code>/points</code> 我的积分；<code>/rank</code> 本周榜
<code>/rank month</code> 月榜；<code>/rank total</code> 总榜

<b>抽奖（管理员）</b>
<code>/newdraw 奖品 | 人数 | YYYY-MM-DD HH:MM | 最低积分</code>
最低积分可不填写；示例：
<code>/newdraw 100 USDT | 3 | 2026-10-25 20:00 | 20</code>
<code>/redraw 抽奖ID</code> 重抽

<b>群管理（管理员）</b>
<code>/pointsrule 每次积分 | 间隔秒数</code>
<code>/addpoints 分数 原因</code>（回复成员消息使用）
<code>/welcome on/off</code>；<code>/setwelcome 欢迎 {name}</code>
<code>/reply add 关键词 | 回复内容</code>
<code>/banword add 违禁词</code>；<code>/mute 10m</code>
<code>/stats</code> 群组统计

时间均按迪拜时间（UTC+4）。""", parse_mode="HTML")


async def new_draw(update, context):
    chat = update.effective_chat
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id):
        await update.message.reply_text("只有群管理员可以在群内创建抽奖。")
        return
    parts = [x.strip() for x in update.message.text.partition(" ")[2].split("|")]
    try:
        if len(parts) not in {3, 4}: raise ValueError
        winners = int(parts[1]); closes = datetime.strptime(parts[2], "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
        minimum = int(parts[3]) if len(parts) == 4 and parts[3] else 0
        if not parts[0] or winners < 1 or minimum < 0 or closes <= now(): raise ValueError
    except ValueError:
        await update.message.reply_text("格式错误。示例：/newdraw 100 USDT | 3 | 2026-10-25 20:00 | 20")
        return
    with closing(db()) as conn:
        draw_id = conn.execute("INSERT INTO draws(chat_id,prize,winner_count,closes_at,created_by,min_points) VALUES (?,?,?,?,?,?)", (chat.id, parts[0], winners, closes.isoformat(), update.effective_user.id, minimum)).lastrowid
        draw = conn.execute("SELECT * FROM draws WHERE id=?", (draw_id,)).fetchone(); conn.commit()
    message = await update.message.reply_text(draw_text(draw), parse_mode="HTML", reply_markup=draw_keyboard(draw_id))
    with closing(db()) as conn:
        conn.execute("UPDATE draws SET message_id=? WHERE id=?", (message.message_id, draw_id)); conn.commit()
    context.job_queue.run_once(close_draw, closes, data={"draw_id": draw_id}, name=f"draw:{draw_id}")


async def join_draw(update, context):
    q = update.callback_query; draw_id = int(q.data.split(":", 1)[1]); user = q.from_user
    with closing(db()) as conn:
        draw = conn.execute("SELECT * FROM draws WHERE id=?", (draw_id,)).fetchone()
        if not draw or draw["status"] != "open":
            await q.answer("该抽奖已结束。", show_alert=True); return
        if draw["min_points"] and member_points(draw["chat_id"], user.id) < draw["min_points"]:
            await q.answer(f"本次抽奖需至少 {draw['min_points']} 积分。", show_alert=True); return
        try:
            conn.execute("INSERT INTO entries(draw_id,user_id,username,display_name,joined_at) VALUES (?,?,?,?,?)", (draw_id, user.id, user.username, user.full_name, now().isoformat())); conn.commit()
        except sqlite3.IntegrityError:
            await q.answer("你已经参加过本次抽奖。", show_alert=True); return
        count = conn.execute("SELECT COUNT(*) FROM entries WHERE draw_id=?", (draw_id,)).fetchone()[0]
    await q.answer("参加成功，祝你好运！", show_alert=True)
    await q.edit_message_text(draw_text(draw, count), parse_mode="HTML", reply_markup=draw_keyboard(draw_id))


async def close_draw(context):
    draw_id = context.job.data["draw_id"]
    with closing(db()) as conn:
        draw = conn.execute("SELECT * FROM draws WHERE id=?", (draw_id,)).fetchone()
        if not draw or draw["status"] != "open": return
        entries = conn.execute("SELECT * FROM entries WHERE draw_id=?", (draw_id,)).fetchall()
        conn.execute("UPDATE draws SET status='closed' WHERE id=?", (draw_id,)); conn.commit()
    if not entries:
        await context.bot.send_message(draw["chat_id"], f"🎰 抽奖 #{draw_id} 已结束。\n\n奖品：{draw['prize']}\n本次无人参加，未产生获奖者。"); return
    winners = random.sample(entries, min(draw["winner_count"], len(entries)))
    names = "\n".join(f"{i+1}. {label(row)}" for i, row in enumerate(winners))
    await context.bot.send_message(draw["chat_id"], f"🎉 <b>盛世豪莱尔抽奖结果</b>\n\n🎁 奖品：{draw['prize']}\n🏆 获奖者：\n{names}\n\n请获奖者联系群管理员领取奖励。", parse_mode="HTML")


async def redraw(update, context):
    chat = update.effective_chat; raw = update.message.text.partition(" ")[2].strip()
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id) or not raw.isdigit():
        await update.message.reply_text("用法：由群管理员发送 /redraw 抽奖ID"); return
    with closing(db()) as conn:
        draw = conn.execute("SELECT * FROM draws WHERE id=? AND chat_id=?", (int(raw), chat.id)).fetchone(); entries = conn.execute("SELECT * FROM entries WHERE draw_id=?", (int(raw),)).fetchall()
    if not draw or not entries:
        await update.message.reply_text("找不到该抽奖，或该抽奖没有参与者。"); return
    winners = random.sample(entries, min(draw["winner_count"], len(entries)))
    await update.message.reply_text("🔄 <b>重抽结果</b>\n\n" + "\n".join(f"{i+1}. {label(row)}" for i, row in enumerate(winners)), parse_mode="HTML")


async def points_command(update, context):
    chat = update.effective_chat
    if chat.type == "private": await update.message.reply_text("请在群内查看积分。"); return
    points = member_points(chat.id, update.effective_user.id); rows = ranking(chat.id, "total", 10000)
    position = next((i+1 for i, row in enumerate(rows) if row["user_id"] == update.effective_user.id), "暂无")
    await update.message.reply_text(f"👤 <b>我的积分</b>\n\n⭐ 总积分：<b>{points}</b> 分\n🏆 当前总榜排名：<b>{position}</b>", parse_mode="HTML")


async def checkin(update, context):
    """One check-in per member per Dubai calendar day."""
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("请在群内进行每日签到。")
        return
    ensure_settings(chat.id)
    user = update.effective_user
    today = now().date().isoformat()
    with closing(db()) as conn:
        settings = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat.id,)).fetchone()
        try:
            conn.execute("INSERT INTO daily_checkins(chat_id,user_id,checkin_date,checked_at) VALUES (?,?,?,?)", (chat.id, user.id, today, now().isoformat()))
            conn.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text("你今天已经签到过了，明天再来吧。")
            return
    total = add_points(chat.id, user, settings["checkin_points"], "每日签到")
    await update.message.reply_text(f"✅ 签到成功！获得 {settings['checkin_points']} 积分。\n⭐ 当前总积分：{total} 分")


async def rank_command(update, context):
    raw = context.args[0].lower() if context.args else "week"
    period = {"week":"week", "month":"month", "total":"total", "周":"week", "月":"month", "总":"total"}.get(raw, "week")
    await update.message.reply_text(ranking_text(update.effective_chat.id, period), parse_mode="HTML", reply_markup=rank_keyboard())


async def points_rule(update, context):
    chat = update.effective_chat
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id):
        await update.message.reply_text("仅群管理员可设置积分规则。"); return
    try:
        per, interval = map(int, [x.strip() for x in update.message.text.partition(" ")[2].split("|")])
        if not (0 <= per <= 10 and 10 <= interval <= 3600): raise ValueError
    except ValueError:
        await update.message.reply_text("用法：/pointsrule 每次积分 | 间隔秒数\n示例：/pointsrule 1 | 60"); return
    ensure_settings(chat.id)
    with closing(db()) as conn:
        conn.execute("UPDATE settings SET points_per_message=?,points_interval_seconds=? WHERE chat_id=?", (per, interval, chat.id)); conn.commit()
    await update.message.reply_text(f"积分规则已保存：每次 {per} 分，发言间隔 {interval} 秒。")


async def add_points_command(update, context):
    chat = update.effective_chat
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id) or not update.message.reply_to_message:
        await update.message.reply_text("请由管理员回复成员消息后使用：/addpoints 分数 原因"); return
    parts = update.message.text.partition(" ")[2].split(maxsplit=1)
    try:
        points = int(parts[0])
        if not (-1000 <= points <= 1000) or points == 0: raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("分数必须是 -1000 至 1000 之间、且不为 0 的整数。"); return
    target = update.message.reply_to_message.from_user; total = add_points(chat.id, target, points, parts[1] if len(parts) > 1 else "管理员调整")
    await update.message.reply_text(f"已为 {target.full_name} {'增加' if points > 0 else '扣除'} {abs(points)} 分，当前积分：{total}。")


async def welcome_setting(update, context):
    chat = update.effective_chat; raw = update.message.text.partition(" ")[2].strip().lower()
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id) or raw not in {"on", "off"}:
        await update.message.reply_text("用法：由管理员发送 /welcome on 或 /welcome off"); return
    ensure_settings(chat.id)
    with closing(db()) as conn:
        conn.execute("UPDATE settings SET welcome_enabled=? WHERE chat_id=?", (raw == "on", chat.id)); conn.commit()
    await update.message.reply_text(f"入群欢迎已{'开启' if raw == 'on' else '关闭'}。")


async def set_welcome(update, context):
    chat = update.effective_chat; text = update.message.text.partition(" ")[2].strip()
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id) or not text:
        await update.message.reply_text("用法：由管理员发送 /setwelcome 欢迎 {name}"); return
    ensure_settings(chat.id)
    with closing(db()) as conn:
        conn.execute("UPDATE settings SET welcome_text=? WHERE chat_id=?", (text, chat.id)); conn.commit()
    await update.message.reply_text("欢迎文案已保存。")


async def reply_setting(update, context):
    chat = update.effective_chat; raw = update.message.text.partition(" ")[2].strip(); action, _, body = raw.partition(" ")
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id):
        await update.message.reply_text("仅群管理员可设置自动回复。"); return
    if action == "add" and "|" in body:
        keyword, response = [x.strip() for x in body.split("|", 1)]
        if keyword and response:
            with closing(db()) as conn:
                conn.execute("INSERT OR REPLACE INTO replies(chat_id,keyword,response) VALUES (?,?,?)", (chat.id, keyword.lower(), response)); conn.commit()
            await update.message.reply_text("自动回复已添加。"); return
    if action == "del" and body.strip():
        with closing(db()) as conn:
            conn.execute("DELETE FROM replies WHERE chat_id=? AND keyword=?", (chat.id, body.strip().lower())); conn.commit()
        await update.message.reply_text("自动回复已删除。"); return
    await update.message.reply_text("用法：/reply add 关键词 | 回复内容\n或 /reply del 关键词")


async def banword_setting(update, context):
    chat = update.effective_chat; raw = update.message.text.partition(" ")[2].strip(); action, _, word = raw.partition(" "); word = word.strip().lower()
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id):
        await update.message.reply_text("仅群管理员可设置违禁词。"); return
    if action in {"add", "del"} and word:
        with closing(db()) as conn:
            if action == "add": conn.execute("INSERT OR IGNORE INTO banned_words(chat_id,word) VALUES (?,?)", (chat.id, word))
            else: conn.execute("DELETE FROM banned_words WHERE chat_id=? AND word=?", (chat.id, word))
            conn.commit()
        await update.message.reply_text(f"违禁词已{'添加' if action == 'add' else '删除'}。"); return
    await update.message.reply_text("用法：/banword add 违禁词\n或 /banword del 违禁词")


async def mute_member(update, context):
    chat = update.effective_chat; raw = update.message.text.partition(" ")[2].strip().lower()
    if chat.type == "private" or not await is_admin(chat, update.effective_user.id) or not update.message.reply_to_message:
        await update.message.reply_text("请由管理员回复成员消息后发送 /mute 10m。"); return
    try: seconds = int(raw[:-1]) * {"m":60,"h":3600,"d":86400}[raw[-1]]
    except (ValueError, KeyError): await update.message.reply_text("时间格式：10m、2h 或 1d。"); return
    target = update.message.reply_to_message.from_user
    await chat.restrict_member(target.id, ChatPermissions(can_send_messages=False), until_date=now() + timedelta(seconds=seconds))
    await update.message.reply_text(f"已禁言 {target.full_name}。")


async def group_stats(update, context):
    chat = update.effective_chat
    if chat.type == "private": await update.message.reply_text("请在群内使用 /stats。"); return
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    with closing(db()) as conn:
        draws = conn.execute("SELECT COUNT(*) FROM draws WHERE chat_id=?", (chat.id,)).fetchone()[0]
        entries = conn.execute("SELECT COUNT(*) FROM entries e JOIN draws d ON d.id=e.draw_id WHERE d.chat_id=?", (chat.id,)).fetchone()[0]
        active = conn.execute("SELECT COUNT(DISTINCT user_id) FROM point_events WHERE chat_id=? AND created_at>=?", (chat.id, today.isoformat())).fetchone()[0]
    await update.message.reply_text(f"📊 <b>群组统计</b>\n\n👥 当前成员：{await chat.get_member_count()}\n🔥 今日活跃：{active}\n🎰 创建抽奖：{draws} 场\n🎉 累计参与：{entries} 人次", parse_mode="HTML")


async def on_new_members(update, context):
    chat = update.effective_chat; ensure_settings(chat.id)
    with closing(db()) as conn: settings = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat.id,)).fetchone()
    if settings and settings["welcome_enabled"]:
        for user in update.message.new_chat_members:
            if not user.is_bot:
                upsert_member(chat.id, user); await update.message.reply_text(settings["welcome_text"].replace("{name}", user.full_name))


def may_award(chat_id, user_id, settings):
    with closing(db()) as conn:
        member = conn.execute("SELECT last_point_at FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    return not (member and member["last_point_at"] and (now() - datetime.fromisoformat(member["last_point_at"])).total_seconds() < settings["points_interval_seconds"])


async def moderate_text(update, context):
    message = update.message; chat = update.effective_chat
    if chat.type == "private" or not message.text or message.from_user.is_bot: return
    ensure_settings(chat.id); text = message.text.strip(); lower = text.lower()
    with closing(db()) as conn:
        settings = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat.id,)).fetchone()
        words = conn.execute("SELECT word FROM banned_words WHERE chat_id=?", (chat.id,)).fetchall()
        reply = conn.execute("SELECT response FROM replies WHERE chat_id=? AND keyword=?", (chat.id, lower)).fetchone()
    upsert_member(chat.id, message.from_user)
    if any(row["word"] in lower for row in words):
        try: await message.delete()
        except Exception: logger.warning("No permission to delete forbidden-word message")
        return
    if reply: await message.reply_text(reply["response"])
    useful = sum(ch.isalnum() or '\u4e00' <= ch <= '\u9fff' for ch in text) >= 2
    if useful and may_award(chat.id, message.from_user.id, settings):
        add_points(chat.id, message.from_user, settings["points_per_message"], "发言积分")
        with closing(db()) as conn:
            conn.execute("UPDATE members SET last_point_at=? WHERE chat_id=? AND user_id=?", (now().isoformat(), chat.id, message.from_user.id)); conn.commit()


async def menu_callback(update, context):
    q = update.callback_query; chat = q.message.chat; key = q.data.split(":", 1)[1]
    if key == "checkin":
        await q.answer()
        # Reuse the same logic as /checkin, with the callback user as the effective user.
        ensure_settings(chat.id)
        today = now().date().isoformat()
        with closing(db()) as conn:
            settings = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat.id,)).fetchone()
            try:
                conn.execute("INSERT INTO daily_checkins(chat_id,user_id,checkin_date,checked_at) VALUES (?,?,?,?)", (chat.id, q.from_user.id, today, now().isoformat()))
                conn.commit()
            except sqlite3.IntegrityError:
                await q.message.reply_text("你今天已经签到过了，明天再来吧。")
                return
        total = add_points(chat.id, q.from_user, settings["checkin_points"], "每日签到")
        await q.message.reply_text(f"✅ 签到成功！获得 {settings['checkin_points']} 积分。\n⭐ 当前总积分：{total} 分")
        return
    if key == "rank":
        await q.answer(); await q.message.reply_text(ranking_text(chat.id), parse_mode="HTML", reply_markup=rank_keyboard()); return
    if key == "me":
        await q.answer(); points = member_points(chat.id, q.from_user.id)
        await q.message.reply_text(f"👤 <b>我的积分</b>\n\n⭐ 总积分：<b>{points}</b> 分", parse_mode="HTML"); return
    if key == "draw":
        with closing(db()) as conn:
            draw = conn.execute("SELECT * FROM draws WHERE chat_id=? AND status='open' ORDER BY closes_at LIMIT 1", (chat.id,)).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM entries WHERE draw_id=?", (draw["id"],)).fetchone()[0] if draw else 0
        await q.answer(); await q.message.reply_text(draw_text(draw, count) if draw else "🎁 当前没有进行中的抽奖。", parse_mode="HTML", reply_markup=draw_keyboard(draw["id"]) if draw else None); return
    if key == "history":
        with closing(db()) as conn: rows = conn.execute("SELECT id,prize,status FROM draws WHERE chat_id=? ORDER BY id DESC LIMIT 5", (chat.id,)).fetchall()
        content = "\n".join(f"#{r['id']}｜{r['prize']}｜{'进行中' if r['status']=='open' else '已结束'}" for r in rows) if rows else "暂无抽奖记录。"
        await q.answer(); await q.message.reply_text("📜 <b>最近抽奖记录</b>\n\n" + content, parse_mode="HTML"); return
    if key == "admin":
        if not await is_admin(chat, q.from_user.id): await q.answer("仅群管理员可打开。", show_alert=True); return
        await q.answer(); await q.message.reply_text("⚙️ <b>管理员面板</b>\n\n创建抽奖：<code>/newdraw 奖品 | 人数 | 时间 | 最低积分</code>\n积分规则：<code>/pointsrule 1 | 60</code>\n其他设置请发送 /help 查看。", parse_mode="HTML"); return
    await q.answer(); await q.message.reply_text("📖 请在群内发送 /help 查看全部功能。")


async def rank_callback(update, context):
    q = update.callback_query; period = q.data.split(":", 1)[1]
    await q.answer(); await q.message.reply_text(ranking_text(q.message.chat.id, period), parse_mode="HTML", reply_markup=rank_keyboard())


async def restore_jobs(app):
    with closing(db()) as conn: draws = conn.execute("SELECT * FROM draws WHERE status='open'").fetchall()
    for draw in draws:
        when = datetime.fromisoformat(draw["closes_at"])
        app.job_queue.run_once(close_draw, 1 if when <= now() else when, data={"draw_id":draw["id"]}, name=f"draw:{draw['id']}")


def main():
    if not TOKEN: raise RuntimeError("请在环境变量 BOT_TOKEN 中填写 Telegram Bot Token。")
    asyncio.set_event_loop(asyncio.new_event_loop())
    init_db()
    app = Application.builder().token(TOKEN).post_init(restore_jobs).build()
    for command, handler in [("start", start), ("help", help_command), ("newdraw", new_draw), ("redraw", redraw), ("points", points_command), ("checkin", checkin), ("rank", rank_command), ("pointsrule", points_rule), ("addpoints", add_points_command), ("welcome", welcome_setting), ("setwelcome", set_welcome), ("reply", reply_setting), ("banword", banword_setting), ("mute", mute_member), ("stats", group_stats)]: app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(join_draw, pattern=r"^join:\d+$"))
    app.add_handler(CallbackQueryHandler(rank_callback, pattern=r"^rank:(week|month|total)$"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, moderate_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__": main()
