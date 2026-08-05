"""Telegram-бот (aiogram 3): капча, команды, обработка заявок на выплату."""
import asyncio
import logging
import random

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app import db
from app.config import ADMIN_IDS, GROUP_CHAT_ID, WEBAPP_URL
from app.payments import build_payout_link

log = logging.getLogger(__name__)


def app_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if WEBAPP_URL:
        buttons.append([InlineKeyboardButton(text="Перейти в приложение",
                                             web_app=WebAppInfo(url=WEBAPP_URL))])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payout_keyboard(payout_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"pa:{payout_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pr:{payout_id}"),
    ]])


# ------------------------- уведомления и решения по заявкам -------------------------

async def notify_admins(bot: Bot, payout_id: int, user: dict, code: str,
                        link: str, wallet: str, shot: bytes | None) -> None:
    text = (f"🆕 <b>Заявка на выплату #{payout_id}</b>\n"
            f"Модератор: <a href='tg://user?id={user['id']}'>{user.get('first_name', '')}</a>"
            f" (@{user.get('username') or '—'}, <code>{user['id']}</code>)\n"
            f"Тег смены: <code>{code}</code>\n"
            f"Отчёт: {link}\n"
            f"Кошелёк: <code>{wallet}</code>")
    for admin_id in ADMIN_IDS:
        try:
            if shot:
                await bot.send_photo(admin_id, BufferedInputFile(shot, "shot.jpg"),
                                     caption=text, reply_markup=payout_keyboard(payout_id))
            else:
                await bot.send_message(admin_id, text, reply_markup=payout_keyboard(payout_id))
        except Exception:
            log.exception("не удалось отправить заявку админу %s", admin_id)


async def approve_payout(bot: Bot, conn, payout, amount: float, admin_id: int) -> dict:
    """Принимает заявку, начисляет баланс и возвращает ссылку на подпись перевода."""
    await db.set_payout_status(conn, payout["id"], "accepted", amount=amount, admin_id=admin_id)
    await db.add_balance(conn, payout["user_id"], amount)
    pay_link = ""
    try:
        pay_link = build_payout_link(payout["wallet"], amount, f"payout #{payout['id']}")
    except ValueError as exc:
        log.error("не удалось сформировать ссылку выплаты: %s", exc)
        for aid in ADMIN_IDS:
            await _safe_send(bot, aid, f"⚠️ Заявка #{payout['id']}: {exc}")
    await _safe_send(bot, payout["user_id"],
                     f"✅ Выплата одобрена: <b>{amount:.2f} TON</b>\n"
                     f"Тег смены: <code>{payout['code']}</code>\n"
                     f"Кошелёк: <code>{payout['wallet']}</code>")
    if GROUP_CHAT_ID:
        await _safe_send(bot, GROUP_CHAT_ID,
                         f"💸 Выплата за модерацию: <b>{amount:.2f} TON</b>\n"
                         f"Тег смены: <code>{payout['code']}</code>")
    return {"pay_link": pay_link}


async def reject_payout(bot: Bot, conn, payout, reason: str, admin_id: int) -> None:
    await db.set_payout_status(conn, payout["id"], "rejected", admin_id=admin_id, reason=reason)
    await _safe_send(bot, payout["user_id"],
                     f"❌ Заявка #{payout['id']} отклонена.\nПричина: {reason}")


async def broadcast(bot: Bot, conn, text: str) -> int:
    sent = 0
    for user_id in await db.all_user_ids(conn):
        if await _safe_send(bot, user_id, text):
            sent += 1
        await asyncio.sleep(0.05)
    return sent


async def _safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except Exception:  # noqa: BLE001 - пользователь мог заблокировать бота
        log.warning("не удалось отправить сообщение в чат %s", chat_id)
        return False


# ------------------------------- хэндлеры -------------------------------

def register(dp: Dispatcher, conn) -> None:
    pending_reason: dict[int, int] = {}   # admin_id -> payout_id
    pending_amount: dict[int, int] = {}   # admin_id -> payout_id

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = message.from_user
        await db.upsert_user(conn, {"id": user.id, "username": user.username,
                                    "first_name": user.first_name})
        row = await db.get_user(conn, user.id)
        if row["is_banned"]:
            return
        if not row["captcha_passed"]:
            a, b = random.randint(2, 9), random.randint(2, 9)
            await conn.execute(
                """INSERT INTO captcha_sessions (user_id, answer, created_at) VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET answer = excluded.answer,
                     created_at = excluded.created_at""",
                (user.id, a + b, db.now_iso()))
            await conn.commit()
            await message.answer(f"🔐 Проверка: сколько будет <b>{a} + {b}</b>?\n"
                                 "Отправь ответ числом.")
            return
        await message.answer("👋 Панель модерации. Открывай приложение и подавай заявки на выплату.",
                             reply_markup=app_keyboard())

    @dp.message(F.chat.type == "private", F.text.regexp(r"^\s*-?\d+\s*$"))
    async def on_number(message: Message) -> None:
        admin_id = message.from_user.id
        if admin_id in pending_amount:
            payout_id = pending_amount.pop(admin_id)
            payout = await db.get_payout(conn, payout_id)
            if not payout or payout["status"] != "pending":
                await message.answer("Заявка уже обработана.")
                return
            amount = float(message.text.strip())
            if amount <= 0:
                await message.answer("Сумма должна быть больше нуля.")
                return
            result = await approve_payout(message.bot, conn, payout, amount, admin_id)
            await message.answer("Заявка принята." + (f"\nПодписать перевод: {result['pay_link']}"
                                                      if result["pay_link"] else ""))
            return

        cur = await conn.execute("SELECT answer FROM captcha_sessions WHERE user_id = ?",
                                 (admin_id,))
        row = await cur.fetchone()
        if not row:
            return
        if int(message.text.strip()) != row["answer"]:
            await message.answer("Неверно, попробуй ещё раз.")
            return
        await db.set_fields(conn, admin_id, captcha_passed=1)
        await conn.execute("DELETE FROM captcha_sessions WHERE user_id = ?", (admin_id,))
        await conn.commit()
        await message.answer("✅ Проверка пройдена. Добро пожаловать!",
                             reply_markup=app_keyboard())

    @dp.message(Command("admin"))
    async def on_admin(message: Message) -> None:
        if message.from_user.id not in ADMIN_IDS:
            return
        rows = await db.list_payouts(conn, status="pending")
        if not rows:
            await message.answer("Заявок в обработке нет.")
            return
        for payout in rows[:20]:
            await message.answer(
                f"#{payout['id']} · user <code>{payout['user_id']}</code>\n"
                f"Тег: <code>{payout['code']}</code>\nКошелёк: <code>{payout['wallet']}</code>\n"
                f"Отчёт: {payout['link']}",
                reply_markup=payout_keyboard(payout["id"]))

    @dp.callback_query(F.data.startswith("pa:"))
    async def on_accept(call: CallbackQuery) -> None:
        if call.from_user.id not in ADMIN_IDS:
            await call.answer("Нет прав", show_alert=True)
            return
        pending_amount[call.from_user.id] = int(call.data.split(":")[1])
        await call.message.answer("Введи сумму выплаты в TON (числом).")
        await call.answer()

    @dp.callback_query(F.data.startswith("pr:"))
    async def on_reject(call: CallbackQuery) -> None:
        if call.from_user.id not in ADMIN_IDS:
            await call.answer("Нет прав", show_alert=True)
            return
        pending_reason[call.from_user.id] = int(call.data.split(":")[1])
        await call.message.answer("Укажи причину отклонения одним сообщением.")
        await call.answer()

    @dp.message(Command("ban"), F.from_user.id.in_(ADMIN_IDS))
    async def on_ban(message: Message) -> None:
        await _set_ban(message, 1, "заблокирован")

    @dp.message(Command("unban"), F.from_user.id.in_(ADMIN_IDS))
    async def on_unban(message: Message) -> None:
        await _set_ban(message, 0, "разблокирован")

    async def _set_ban(message: Message, value: int, word: str) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("Использование: /ban &lt;user_id&gt;")
            return
        await db.set_fields(conn, int(parts[1]), is_banned=value)
        await message.answer(f"Пользователь {parts[1]} {word}.")

    @dp.message(Command("broadcast"), F.from_user.id.in_(ADMIN_IDS))
    async def on_broadcast(message: Message) -> None:
        text = (message.text or "").partition(" ")[2].strip()
        if not text:
            await message.answer("Использование: /broadcast текст")
            return
        sent = await broadcast(message.bot, conn, text)
        await message.answer(f"Отправлено: {sent}")

    @dp.message(Command("topm", "topn"))
    async def on_top(message: Message) -> None:
        period = "month" if (message.text or "").startswith("/topm") else "week"
        rows = await db.leaderboard(conn, period)
        if not rows:
            await message.answer("Данных пока нет.")
            return
        title = "месяц" if period == "month" else "неделю"
        lines = [f"<b>Топ за {title}</b>"]
        lines += [f"{i}. {row['name'] or 'Модератор'} — {row['amount']:.2f} TON"
                  for i, row in enumerate(rows[:10], start=1)]
        await message.answer("\n".join(lines))

    @dp.message(Command("stat"))
    async def on_stat(message: Message) -> None:
        cur = await conn.execute("SELECT COUNT(*) AS c FROM users")
        users = (await cur.fetchone())["c"]
        cur = await conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS s FROM payouts WHERE status = 'accepted'")
        row = await cur.fetchone()
        await message.answer(f"👥 Пользователей: {users}\n"
                             f"💸 Выплат: {row['c']} на {row['s']:.2f} TON")

    @dp.message(F.chat.type == "private", F.text)
    async def on_text(message: Message) -> None:
        admin_id = message.from_user.id
        if admin_id in pending_reason:
            payout_id = pending_reason.pop(admin_id)
            payout = await db.get_payout(conn, payout_id)
            if not payout or payout["status"] != "pending":
                await message.answer("Заявка уже обработана.")
                return
            await reject_payout(message.bot, conn, payout, message.text.strip()[:200], admin_id)
            await message.answer("Заявка отклонена.")
