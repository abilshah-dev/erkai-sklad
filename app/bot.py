"""Telegram-бот: регистрация, меню по ролям, кнопки подтверждения."""
from __future__ import annotations

import logging

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp,
                      ReplyKeyboardMarkup, Update, WebAppInfo)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from . import config, db, notify

log = logging.getLogger(__name__)

# Тексты кнопок нижнего меню
B_ORDER, B_CAR, B_RETURN = "🛒 Заявка на склад", "🚚 Моя машина", "↩️ Вернуть остатки"
B_NEW = "📥 Новые заявки"
B_JOURNAL, B_AGENTS, B_INVITE = "📒 Журнал движений", "🚚 Товар у агентов", "➕ Пригласить сотрудника"


def menu(role: str) -> ReplyKeyboardMarkup:
    if role == "agent":
        rows = [[B_ORDER], [B_CAR, B_RETURN]]
    elif role == "storekeeper":
        rows = [[B_NEW]]
    else:  # owner, accountant
        rows = [[B_JOURNAL, B_AGENTS], [B_NEW, B_INVITE]]
    return ReplyKeyboardMarkup([[KeyboardButton(t) for t in r] for r in rows], resize_keyboard=True)


def open_button(text: str, view: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, web_app=WebAppInfo(f"{config.WEBAPP_URL}?view={view}"))]])


async def current_user(update: Update):
    u = await db.user_by_tg(update.effective_user.id)
    return u if u and u["active"] else None


async def greet(update: Update, ctx: ContextTypes.DEFAULT_TYPE, u: dict):
    # Кнопка «Открыть» слева от поля ввода — открывает окно склада
    await ctx.bot.set_chat_menu_button(
        chat_id=update.effective_chat.id,
        menu_button=MenuButtonWebApp("Склад", WebAppInfo(config.WEBAPP_URL)))
    await update.effective_message.reply_text(
        f"Здравствуйте, {u['full_name']}! Вы вошли как: {db.ROLE_NAMES[u['role']]}.\n"
        f"Окно склада открывается кнопкой «Склад» слева от поля ввода.",
        reply_markup=menu(u["role"]))


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    u = await db.user_by_tg(tg.id)
    if ctx.args:  # ссылка-приглашение t.me/bot?start=КОД
        try:
            u = await db.use_invite(ctx.args[0], tg.id)
        except db.DbError as e:
            await update.message.reply_text(str(e))
            return
    elif not u and config.OWNER_TG_ID and tg.id == config.OWNER_TG_ID:
        u = await db.ensure_owner(tg.id, tg.full_name)
    if not u:
        await update.message.reply_text(f"{config.COMPANY_NAME}: бот для сотрудников.\nОтправьте код приглашения.")
        return
    if not u["active"]:
        await update.message.reply_text("Доступ закрыт. Обратитесь к бухгалтеру.")
        return
    await greet(update, ctx, u)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    u = await current_user(update)

    if not u:  # незарегистрированный пользователь прислал код
        try:
            u = await db.use_invite(text, update.effective_user.id)
        except db.DbError:
            await update.message.reply_text("Код не подошёл. Попросите у бухгалтера новую ссылку-приглашение.")
            return
        await greet(update, ctx, u)
        return

    # Ожидаем имя нового сотрудника (шаг приглашения)
    if ctx.user_data.get("invite_role") and text not in (B_JOURNAL, B_AGENTS, B_NEW, B_INVITE):
        role = ctx.user_data.pop("invite_role")
        code = await db.create_invite(role, text, u["id"])
        link = f"https://t.me/{ctx.bot.username}?start={code}"
        await update.message.reply_text(
            f"Приглашение для «{text}» ({db.ROLE_NAMES[role]}).\nПерешлите сотруднику ссылку:\n{link}\n\n"
            f"Или код: {code}")
        return

    try:
        if text == B_ORDER and u["role"] == "agent":
            await update.message.reply_text("Соберите заявку в корзине:", reply_markup=open_button("🛒 Открыть корзину", "order"))
        elif text == B_CAR and u["role"] == "agent":
            await show_car(update, u)
        elif text == B_RETURN and u["role"] == "agent":
            await ask_return(update, u)
        elif text == B_NEW and u["role"] in ("storekeeper", "owner", "accountant"):
            await show_open_requests(update)
        elif text == B_JOURNAL and u["role"] in ("owner", "accountant"):
            await show_journal(update)
        elif text == B_AGENTS and u["role"] in ("owner", "accountant"):
            await show_agents(update)
        elif text == B_INVITE and u["role"] in ("owner", "accountant"):
            await ask_invite_role(update, u)
        else:
            await update.message.reply_text("Выберите действие в меню.", reply_markup=menu(u["role"]))
    except db.DbError as e:
        await update.message.reply_text(str(e))


async def show_car(update: Update, u: dict):
    rows = await db.car(u["id"])
    limit = float(await db.setting("agent_stock_limit", 0) or 0)
    total = sum(float(r["value"]) for r in rows)
    if not rows:
        await update.message.reply_text("В машине пусто.")
        return
    lines = [f"• {r['product']} — {r['qty']} {r['unit']}" for r in rows]
    tail = f"\nЛимит: {db.money(limit)}" if limit else ""
    await update.message.reply_text("🚚 В машине:\n" + "\n".join(lines) + f"\n\nНа сумму: {db.money(total)}{tail}")


async def ask_return(update: Update, u: dict):
    rows = await db.car(u["id"])
    if not rows:
        await update.message.reply_text("В машине пусто — возвращать нечего.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Вернуть всё", callback_data="ret:all")],
        [InlineKeyboardButton("✏️ Выбрать позиции", web_app=WebAppInfo(f"{config.WEBAPP_URL}?view=car"))],
    ])
    await update.message.reply_text(f"В машине {len(rows)} поз. Что возвращаем?", reply_markup=kb)


async def show_open_requests(update: Update):
    reqs = await db.open_requests()
    if not reqs:
        await update.message.reply_text("Новых заявок нет.")
        return
    for r in reqs:
        await update.message.reply_text(notify.request_text(r), reply_markup=notify.request_keyboard(r))


async def show_journal(update: Update):
    rows = await db.journal(15)
    if not rows:
        await update.message.reply_text("Движений пока нет.")
        return
    lines = []
    for m in rows:
        ts = m["ts"][5:16].replace("T", " ")
        route = f"{m['from_name'] or '1С'} → {m['to_name'] or 'списание'}"
        lines.append(f"{ts} · {m['doc_no']} · {m['product']} {m['qty']} {m['unit']}\n   {route}")
    await update.message.reply_text("📒 Последние движения:\n\n" + "\n".join(lines),
                                    reply_markup=open_button("Весь журнал", "journal"))


async def show_agents(update: Update):
    agents = await db.users_by_roles(["agent"])
    stock = await db.all_cars()
    lines = []
    for a in agents:
        val = sum(float(s["value"]) for s in stock if s["agent_id"] == a["id"])
        lines.append(f"• {a['full_name']} — {db.money(val)}")
    await update.message.reply_text("🚚 Товар на руках у агентов:\n" + ("\n".join(lines) or "агентов нет"),
                                    reply_markup=open_button("👥 База клиентов", "clients"))


async def ask_invite_role(update: Update, u: dict):
    roles = [("agent", "Торговый агент"), ("storekeeper", "Складовщик")]
    if u["role"] == "owner":
        roles.append(("accountant", "Бухгалтер"))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=f"inv:{r}")] for r, t in roles])
    await update.message.reply_text("Кого приглашаем?", reply_markup=kb)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = await current_user(update)
    if not u:
        await q.answer("Нет доступа", show_alert=True)
        return
    kind, _, arg = q.data.partition(":")
    try:
        if kind == "rq":  # складовщик: подтвердить как в заявке
            if u["role"] not in ("storekeeper", "owner", "accountant"):
                await q.answer("Подтверждает складовщик", show_alert=True)
                return
            res = await db.confirm_request(arg, u["id"], None)
            r = await db.get_request(arg)
            await q.edit_message_text(notify.request_text(r).split("\n\n")[0] +
                                      f"\n\n✅ Подтверждено: {u['full_name']}\n\n{notify.request_lines(r, actual=True)}")
            await q.answer("Готово")
            await notify.confirmed(ctx.bot, res["id"], u["full_name"])
        elif kind == "ret" and u["role"] == "agent":
            items = [{"product_id": r["product_id"], "qty": r["qty"]} for r in await db.car(u["id"])]
            res = await db.create_request(u["id"], "return", items)
            await q.edit_message_text(f"Возврат {res['no']} создан.")
            await q.answer()
            await notify.new_request(ctx.bot, res["id"])
        elif kind == "inv" and u["role"] in ("owner", "accountant"):
            ctx.user_data["invite_role"] = arg
            await q.edit_message_text(f"{db.ROLE_NAMES[arg]}: напишите имя и фамилию сотрудника.")
            await q.answer()
        else:
            await q.answer()
    except db.DbError as e:
        await q.answer(str(e), show_alert=True)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Ошибка в обработчике", exc_info=ctx.error)


def setup(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
