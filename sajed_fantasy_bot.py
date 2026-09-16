import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= إعدادات =================
# لا تضع التوكن هنا مباشرة — خزّنه في متغير بيئة TELEGRAM_BOT_TOKEN.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# آيدي الأدمن الوحيد المسموح له بفتح لوحة الإدارة.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
# رابط الـ Mini App (نفس السيرفر Flask الذي يخدم index.html) — يجب أن يكون https.
WEBAPP_URL = os.environ.get("MINI_APP_URL", "https://example.com")

DB_PATH = os.environ.get("SAJED_BOT_DB_PATH", "sajed_fantasy_bot.db")

CHANNEL_URL = "https://t.me/FPL_sajed"
DEVELOPER_URL = "https://t.me/sdsr10"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ================= قاعدة البيانات =================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            title TEXT,
            added_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def upsert_user(user_id: int, username: str, first_name: str, last_name: str):
    conn = db()
    existing = conn.execute("SELECT 1 FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET username=?, first_name=?, last_name=? WHERE telegram_id=?",
            (username, first_name, last_name, user_id),
        )
    else:
        conn.execute(
            "INSERT INTO users (telegram_id, username, first_name, last_name, joined_at) VALUES (?,?,?,?,?)",
            (user_id, username, first_name, last_name, datetime.now().isoformat()),
        )
    conn.commit()
    conn.close()


# ---------- قنوات الاشتراك الإجباري (منسوخة من نظام بوت التعزيز) ----------
def get_required_channels():
    conn = db()
    rows = conn.execute("SELECT * FROM required_channels ORDER BY id").fetchall()
    conn.close()
    return rows


def add_required_channel(chat_id: str, title: str):
    conn = db()
    exists = conn.execute(
        "SELECT 1 FROM required_channels WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO required_channels (chat_id, title, added_at) VALUES (?,?,?)",
            (chat_id, title, datetime.now().isoformat()),
        )
        conn.commit()
    conn.close()
    return not exists


def remove_required_channel(row_id: int):
    conn = db()
    conn.execute("DELETE FROM required_channels WHERE id=?", (row_id,))
    conn.commit()
    conn.close()


async def check_subscription(user_id: int):
    """يرجّع لستة القنوات التي المستخدم غير مشترك فيها بعد."""
    channels = get_required_channels()
    not_joined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return not_joined


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


# ================= لوحات المفاتيح =================
def subscription_kb(channels) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ch in channels:
        raw = (ch["chat_id"] or "").strip()
        if raw.startswith("@"):
            url = f"https://t.me/{raw[1:]}"
        elif raw.startswith("http://") or raw.startswith("https://"):
            url = raw
        elif raw.startswith("-"):
            url = None
        else:
            url = f"https://t.me/{raw}"
        if url:
            b.button(text=f"📢 {ch['title']}", url=url)
        else:
            b.button(text=f"📢 {ch['title']}", callback_data="noop")
    b.button(text="✅ تحقق من الاشتراك", callback_data="check_sub")
    b.adjust(1)
    return b.as_markup()


def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📱 فتح SAJED FANTASY", web_app=WebAppInfo(url=WEBAPP_URL))
    b.button(text="📢 قناة البوت", url=CHANNEL_URL)
    b.button(text="👨‍💻 مطور البوت", url=DEVELOPER_URL)
    if is_admin(user_id):
        b.button(text="⚙️ لوحة الإدارة", callback_data="admin_panel")
    b.adjust(1)
    return b.as_markup()


def admin_panel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 إحصائيات البوت", callback_data="admin_stats")
    b.button(text="👥 المستخدمين", callback_data="admin_users_0")
    b.button(text="📈 إحصائيات النشاط", callback_data="admin_activity")
    b.button(text="🔒 إدارة الاشتراك الإجباري", callback_data="admin_sub_manage")
    b.button(text="📣 إذاعة", callback_data="admin_broadcast")
    b.button(text="📱 فتح Mini App", web_app=WebAppInfo(url=WEBAPP_URL))
    b.button(text="🔙 الرئيسية", callback_data="menu_home")
    b.adjust(1)
    return b.as_markup()


def sub_manage_kb() -> InlineKeyboardMarkup:
    channels = get_required_channels()
    b = InlineKeyboardBuilder()
    for ch in channels:
        b.button(text=f"📢 {ch['title']}", callback_data="noop")
    b.button(text="➕ إضافة قناة", callback_data="admin_add_channel")
    b.button(text="🗑 إزالة قناة", callback_data="admin_remove_channel")
    b.button(text="🔙 رجوع", callback_data="admin_panel")
    b.adjust(1)
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 رجوع", callback_data="admin_sub_manage")
    b.adjust(1)
    return b.as_markup()


def users_page_kb(page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    nav_row = []
    if page > 0:
        nav_row.append(("⬅️ السابق", f"admin_users_{page-1}"))
    if has_next:
        nav_row.append(("التالي ➡️", f"admin_users_{page+1}"))
    for text, cb in nav_row:
        b.button(text=text, callback_data=cb)
    if nav_row:
        b.adjust(len(nav_row))
    b.row(InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_panel"))
    return b.as_markup()


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ إرسال الآن", callback_data="admin_broadcast_send")
    b.button(text="❌ إلغاء", callback_data="admin_panel")
    b.adjust(1)
    return b.as_markup()


# ================= إحصائيات =================
def get_stats():
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    today = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE date(joined_at)=date('now')"
    ).fetchone()["c"]
    week = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE date(joined_at)>=date('now','-6 days')"
    ).fetchone()["c"]
    month = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE date(joined_at)>=date('now','-29 days')"
    ).fetchone()["c"]
    conn.close()
    return {"total": total, "today": today, "week": week, "month": month}


def get_daily_activity(days: int = 7):
    conn = db()
    rows = conn.execute(
        """
        SELECT date(joined_at) d, COUNT(*) c
        FROM users
        WHERE date(joined_at) >= date('now', ?)
        GROUP BY date(joined_at)
        ORDER BY d DESC
        """,
        (f"-{days-1} days",),
    ).fetchall()
    conn.close()
    return rows


def get_users_page(page: int, page_size: int = 10):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
        (page_size + 1, page * page_size),
    ).fetchall()
    conn.close()
    has_next = len(rows) > page_size
    return rows[:page_size], has_next


def get_all_user_ids():
    conn = db()
    rows = conn.execute("SELECT telegram_id FROM users").fetchall()
    conn.close()
    return [r["telegram_id"] for r in rows]


# ================= حالات FSM =================
class AdminAddChannel(StatesGroup):
    waiting_id = State()


class AdminBroadcast(StatesGroup):
    waiting_message = State()
    confirming = State()


# ================= الاشتراك الإجباري (Middleware — منسوخ من بوت التعزيز) =================
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        # الأدمن معفى دائماً من شرط الاشتراك
        if is_admin(user.id):
            return await handler(event, data)

        # /start تتولى فحص الاشتراك بنفسها
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        # زر التحقق من الاشتراك لازم يمر دائماً
        if isinstance(event, CallbackQuery) and event.data in ("check_sub", "noop"):
            return await handler(event, data)

        channels = get_required_channels()
        if channels:
            not_joined = await check_subscription(user.id)
            if not_joined:
                text = "🔒 اشتراك إجباري\n\nانضم إلى القنوات التالية حتى تتمكن من استخدام البوت.\n\nبعد الاشتراك اضغط «✅ تحقق من الاشتراك»."
                markup = subscription_kb(not_joined)
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ يجب الاشتراك بالقنوات أولاً", show_alert=True)
                    await event.message.answer(text, reply_markup=markup)
                else:
                    await event.answer(text, reply_markup=markup)
                return
        return await handler(event, data)


router.message.outer_middleware(SubscriptionMiddleware())
router.callback_query.outer_middleware(SubscriptionMiddleware())


# ================= /start =================
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    not_joined = await check_subscription(user_id)
    if not_joined:
        text = "🔒 اشتراك إجباري\n\nانضم إلى القنوات التالية حتى تتمكن من استخدام البوت.\n\nبعد الاشتراك اضغط «✅ تحقق من الاشتراك»."
        await message.answer(text, reply_markup=subscription_kb(not_joined))
        return

    await show_home(message.from_user, message.answer)


async def show_home(tg_user, send):
    upsert_user(tg_user.id, tg_user.username, tg_user.first_name, tg_user.last_name)
    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "⚽ SAJED FANTASY\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "مرحباً بك 👋\n\n"
        "إدارة فريقك، دورياتك وإحصائياتك من مكان واحد."
    )
    await send(text, reply_markup=main_menu_kb(tg_user.id))


@router.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery, state: FSMContext):
    not_joined = await check_subscription(callback.from_user.id)
    if not_joined:
        await callback.answer("❗️ ما زلت غير مشترك بالقناة، اشترك ثم أعد المحاولة", show_alert=True)
        return
    await callback.answer("✅ تم التحقق بنجاح")
    await show_home(callback.from_user, callback.message.answer)


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "menu_home")
async def menu_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "⚽ SAJED FANTASY\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "مرحباً بك 👋\n\n"
        "إدارة فريقك، دورياتك وإحصائياتك من مكان واحد."
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb(callback.from_user.id))
    await callback.answer()


# ================= لوحة الأدمن =================
@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 لوحة إدارة SAJED FANTASY", reply_markup=admin_panel_kb())


@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text("🛠 لوحة إدارة SAJED FANTASY", reply_markup=admin_panel_kb())
    await callback.answer()


@router.callback_query(F.data == "admin_sub_manage")
async def admin_sub_manage(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    channels = get_required_channels()
    lines = ["🔒 إدارة الاشتراك الإجباري\n"]
    if channels:
        lines.append("القنوات المطلوبة حالياً:\n")
        for ch in channels:
            lines.append(f"📢 {ch['title']}\n{ch['chat_id']}")
    else:
        lines.append("لا توجد قنوات اشتراك إجباري مضافة حالياً.")
    await callback.message.edit_text("\n".join(lines), reply_markup=sub_manage_kb())
    await callback.answer()


@router.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminAddChannel.waiting_id)
    await callback.message.answer(
        "➕ أرسل يوزرنيم القناة (مثال: @FPL_sajed) أو Chat ID (مثال: -1001234567890).\n"
        "⚠️ تأكد أن البوت أدمن في تلك القناة حتى يستطيع التحقق من الاشتراك."
    )
    await callback.answer()


@router.message(AdminAddChannel.waiting_id)
async def admin_add_channel_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    try:
        chat = await bot.get_chat(raw)
    except Exception:
        await message.answer("❗️ لم أستطع الوصول لهذه القناة. تأكد من اليوزر/الأيدي وأن البوت أدمن فيها، ثم أعد المحاولة.")
        return
    added = add_required_channel(raw, chat.title or raw)
    await state.clear()
    if added:
        await message.answer(f"✅ تمت إضافة «{chat.title or raw}» لقائمة قنوات الاشتراك الإجباري.", reply_markup=sub_manage_kb())
    else:
        await message.answer("ℹ️ هذه القناة مضافة مسبقاً.", reply_markup=sub_manage_kb())


@router.callback_query(F.data == "admin_remove_channel")
async def admin_remove_channel_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    channels = get_required_channels()
    if not channels:
        await callback.message.edit_text("لا توجد قنوات اشتراك إجباري مضافة حالياً.", reply_markup=back_kb())
        await callback.answer()
        return
    b = InlineKeyboardBuilder()
    for ch in channels:
        b.button(text=f"🗑 {ch['title']}", callback_data=f"delchannel_{ch['id']}")
    b.button(text="🔙 رجوع", callback_data="admin_sub_manage")
    b.adjust(1)
    await callback.message.edit_text("🗑 اختر قناة الاشتراك الإجباري التي تريد إزالتها:", reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    s = get_stats()
    text = (
        "📊 إحصائيات البوت\n\n"
        f"👥 إجمالي المستخدمين:\n{s['total']}\n\n"
        f"🆕 المستخدمون اليوم:\n{s['today']}\n\n"
        f"📅 المستخدمون هذا الأسبوع:\n{s['week']}\n\n"
        f"📅 المستخدمون هذا الشهر:\n{s['month']}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="🔄 تحديث", callback_data="admin_stats")
    b.button(text="🔙 رجوع", callback_data="admin_panel")
    b.adjust(1)
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin_activity")
async def admin_activity(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows = get_daily_activity(7)
    lines = ["📈 إحصائيات النشاط (آخر 7 أيام)\n"]
    if rows:
        for r in rows:
            lines.append(f"{r['d']}: {r['c']} مستخدم جديد")
    else:
        lines.append("لا يوجد نشاط تسجيل خلال آخر 7 أيام.")
    b = InlineKeyboardBuilder()
    b.button(text="🔄 تحديث", callback_data="admin_activity")
    b.button(text="🔙 رجوع", callback_data="admin_panel")
    b.adjust(1)
    await callback.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin_users_"))
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split("_")[-1])
    users, has_next = get_users_page(page)
    total = get_stats()["total"]
    lines = [f"👥 المستخدمون (الإجمالي: {total})\n"]
    if not users:
        lines.append("لا يوجد مستخدمون في هذه الصفحة.")
    for u in users:
        uname = f"@{u['username']}" if u["username"] else "بدون يوزرنيم"
        name = " ".join(filter(None, [u["first_name"], u["last_name"]])) or "بدون اسم"
        lines.append(
            f"—\nID: {u['telegram_id']}\nالاسم: {name}\nاليوزرنيم: {uname}\nتاريخ التسجيل: {u['joined_at']}"
        )
    await callback.message.edit_text("\n".join(lines), reply_markup=users_page_kb(page, has_next))
    await callback.answer()


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminBroadcast.waiting_message)
    await callback.message.answer("📣 أرسل الآن الرسالة التي تريد إذاعتها لجميع المستخدمين المسجلين.")
    await callback.answer()


@router.message(AdminBroadcast.waiting_message)
async def admin_broadcast_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(AdminBroadcast.confirming)
    count = len(get_all_user_ids())
    await message.answer(
        f"⚠️ سيتم إرسال هذه الرسالة إلى {count} مستخدم. هل تريد المتابعة؟",
        reply_markup=broadcast_confirm_kb(),
    )


@router.callback_query(AdminBroadcast.confirming, F.data == "admin_broadcast_send")
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    src_chat_id = data.get("chat_id")
    src_message_id = data.get("message_id")
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("⏳ جاري إرسال الإذاعة...")

    user_ids = get_all_user_ids()
    success = 0
    failed = 0
    for uid in user_ids:
        while True:
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
                success += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except (TelegramForbiddenError, TelegramBadRequest):
                failed += 1
                break
            except Exception:
                failed += 1
                break
        await asyncio.sleep(0.05)

    await callback.message.answer(f"✅ تم الإرسال\n\nنجاح: {success}\nفشل: {failed}", reply_markup=admin_panel_kb())


@router.callback_query(F.data.startswith("delchannel_"))
async def admin_remove_channel_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    row_id = int(callback.data.split("_", 1)[1])
    remove_required_channel(row_id)
    await callback.answer("تم الحذف")
    await admin_sub_manage(callback)


# ================= تشغيل البوت =================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("لازم تحدد متغير البيئة TELEGRAM_BOT_TOKEN قبل تشغيل البوت.")
    if ADMIN_ID == 0:
        logging.warning("لم يتم تحديد ADMIN_ID — لوحة الإدارة لن تظهر لأحد حتى تضبط متغير البيئة ADMIN_ID.")
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
