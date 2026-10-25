"""Сообщения, которые бот рассылает сотрудникам. Используются и ботом, и API."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from . import config, db

log = logging.getLogger(__name__)


def request_lines(r: dict, actual: bool = False) -> str:
    lines = []
    for it in r["request_items"]:
        p = it["products"]
        qty = it["qty_actual"] if actual else it["qty_requested"]
        mark = ""
        if actual and it["qty_actual"] is not None and it["qty_actual"] < it["qty_requested"]:
            mark = f" (просили {it['qty_requested']})"
        lines.append(f"• {p['name']} — {qty} {p['unit']}{mark}")
    return "\n".join(lines)


def request_text(r: dict) -> str:
    title = "📦 Заявка на выдачу" if r["kind"] == "issue" else "↩️ Возврат на склад"
    return f"{title} {r['no']}\nАгент: {r['agent_name']}\n\n{request_lines(r)}"


def request_keyboard(r: dict) -> InlineKeyboardMarkup:
    ok = "✅ Выдать как в заявке" if r["kind"] == "issue" else "✅ Принять как в заявке"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ok, callback_data=f"rq:{r['id']}")],
        [InlineKeyboardButton("✏️ Изменить количество",
                              web_app=WebAppInfo(f"{config.WEBAPP_URL}?view=confirm&id={r['id']}"))],
    ])


async def _send(bot: Bot, tg_id: int, text: str, **kw):
    try:
        await bot.send_message(tg_id, text, **kw)
    except Exception as e:  # сотрудник мог заблокировать бота — не роняем остальную рассылку
        log.warning("Не удалось отправить %s: %s", tg_id, e)


async def new_request(bot: Bot, request_id: str):
    """Складовщикам: новая заявка с кнопками подтверждения. Агенту: заявка принята."""
    r = await db.get_request(request_id)
    for u in await db.users_by_roles(["storekeeper"]):
        await _send(bot, u["tg_id"], request_text(r), reply_markup=request_keyboard(r))
    agent = await db.user_by_id(r["agent_id"])
    if agent:
        await _send(bot, agent["tg_id"], f"Заявка {r['no']} отправлена складовщику. Сообщу, когда подтвердит.")


async def confirmed(bot: Bot, request_id: str, by_name: str):
    """Агенту и бухгалтеру: что фактически выдано или принято."""
    r = await db.get_request(request_id)
    verb = "выдано" if r["kind"] == "issue" else "принято на склад"
    body = f"{r['no']}: {verb} ({by_name})\n\n{request_lines(r, actual=True)}"
    agent = await db.user_by_id(r["agent_id"])
    if agent:
        tail = "\n\nТовар числится за вами." if r["kind"] == "issue" else ""
        await _send(bot, agent["tg_id"], "✅ " + body + tail)
    if await db.setting("notify_accountant_on_moves", True):
        for u in await db.users_by_roles(["accountant"]):
            await _send(bot, u["tg_id"], f"🧾 Агент {r['agent_name']} · " + body)


# ---------- Этап 2: продажи, оплаты, деньги ----------

PAY_TEXT = {"cash": "наличными", "transfer": "переводом", "debt": "в долг", "part": "частично"}


async def _accountants():
    """Бухгалтеры; если бухгалтера нет — владельцы."""
    return await db.users_by_roles(["accountant"]) or await db.users_by_roles(["owner"])


def sale_lines(s: dict, mark_over: bool = False) -> str:
    out = []
    for it in s["items"]:
        d = float(it["discount_pct"])
        disc = f", скидка {d:g}%" if d else ""
        if mark_over and d > float(it["max_discount"]):
            disc += f" ⚠️ лимит {float(it['max_discount']):g}%"
        out.append(f"• {it['name']} — {it['qty']} {it['unit']}{disc} = {db.money(it['amount'])}")
    return "\n".join(out)


async def sale_done(bot: Bot, sale_id: str):
    """Агенту — PDF-накладная, бухгалтеру — копия продажи."""
    from telegram import InputFile

    from . import invoice
    s = await db.get_sale(sale_id)
    agent = await db.user_by_id(s["agent_id"])
    total, paid = float(s["total"]), float(s["paid_now"])
    pay = PAY_TEXT[s["pay_type"]] if s["pay_type"] != "part" else f"оплачено {db.money(paid)}"
    debt = f", в долг {db.money(total - paid)}" if total - paid > 0.004 else ""
    head = f"🧾 Продажа №{s['no']} · {s['client_name']}\n{db.money(total)} ({pay}{debt})"
    if agent:
        try:
            pdf = invoice.build(s)
            await bot.send_document(agent["tg_id"], InputFile(pdf, filename=f"Накладная_{s['no']}.pdf"),
                                    caption=head + "\n\nПерешлите накладную клиенту.")
        except Exception as e:
            log.exception("Накладная №%s не отправлена: %s", s["no"], e)
            await _send(bot, agent["tg_id"], head)
    if await db.setting("notify_accountant_on_moves", True):
        for u in await _accountants():
            await _send(bot, u["tg_id"], f"{head}\nАгент: {s['agent_name']}\n\n{sale_lines(s)}")


async def sale_pending(bot: Bot, sale_id: str):
    """Владельцу — запрос на скидку с кнопками."""
    s = await db.get_sale(sale_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Одобрить", callback_data=f"sale:ok:{s['id']}"),
                                InlineKeyboardButton("❌ Отклонить", callback_data=f"sale:no:{s['id']}")]])
    text = (f"🏷 Запрос на скидку · продажа №{s['no']}\nАгент: {s['agent_name']}\nКлиент: {s['client_name']}\n\n"
            f"{sale_lines(s, mark_over=True)}\n\nПо прайсу: {db.money(s['base_total'])}\n"
            f"Со скидкой: {db.money(s['total'])} (скидка {db.money(float(s['base_total']) - float(s['total']))})\n"
            f"Оплата: {PAY_TEXT[s['pay_type']]}")
    for u in await db.users_by_roles(["owner"]):
        await _send(bot, u["tg_id"], text, reply_markup=kb)
    agent = await db.user_by_id(s["agent_id"])
    if agent:
        await _send(bot, agent["tg_id"], f"Продажа №{s['no']}: скидка выше лимита — отправил владельцу на согласование.")


async def sale_decided(bot: Bot, res: dict, by_name: str):
    if res["approved"]:
        await sale_done(bot, res["id"])
        return
    agent = await db.user_by_id(res["agent_id"])
    if agent:
        await _send(bot, agent["tg_id"],
                    f"❌ {by_name} отклонил скидку по продаже №{res['no']}. Товар остался в машине — "
                    f"оформите продажу заново с меньшей скидкой.")


async def payment_taken(bot: Bot, user: dict, client: dict, res: dict, method: str):
    if await db.setting("notify_accountant_on_moves", True):
        for u in await _accountants():
            if u["id"] != user["id"]:
                await _send(bot, u["tg_id"],
                            f"💵 {user['full_name']} принял оплату {db.money(res['paid'])} ({PAY_TEXT[method]}) "
                            f"от {client['name']}. Остаток долга: {db.money(res['debt_left'])}")


async def handover_new(bot: Bot, res: dict, agent_name: str):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Деньги получены", callback_data=f"ho:ok:{res['id']}")],
                               [InlineKeyboardButton("❌ Не получены", callback_data=f"ho:no:{res['id']}")]])
    for u in await _accountants():
        await _send(bot, u["tg_id"], f"💰 {agent_name} сдаёт наличные: {db.money(res['amount'])} (сдача №{res['no']})",
                    reply_markup=kb)


async def handover_decided(bot: Bot, res: dict, by_name: str):
    agent = await db.user_by_id(res["agent_id"])
    if not agent:
        return
    if res["ok"]:
        await _send(bot, agent["tg_id"], f"✅ {by_name} подтвердил: получено {db.money(res['amount'])}.")
    else:
        await _send(bot, agent["tg_id"], f"⚠️ {by_name} не подтвердил получение {db.money(res['amount'])}. "
                                         f"Деньги снова числятся за вами — свяжитесь с бухгалтером.")


async def summary_text() -> str:
    s = await db.owner_summary()
    now = datetime.now(timezone(timedelta(hours=6)))
    lines = [f"📊 Сводка на {now:%d.%m %H:%M}",
             f"Продажи сегодня: {db.money(s['sales_today'])} ({s['sales_count']} накл.)",
             f"Отдано скидками: {db.money(s['discount_today'])}",
             f"Долги клиентов: {db.money(s['debts_total'])}",
             f"  из них просрочено: {db.money(s['debts_overdue'])}",
             f"Товар у агентов: {db.money(s['goods_at_agents'])}",
             f"Деньги у агентов: {db.money(s['cash_at_agents'])}"]
    if s["pending_approvals"]:
        lines.append(f"⏳ Ждут вашего согласования: {s['pending_approvals']}")
    if s["agents"]:
        lines.append("\nПо агентам (продажи · в машине · деньги · долги):")
        for a in s["agents"]:
            lines.append(f"• {a['name']}: {db.money(a['sales_today'])} · {db.money(a['car'])} · "
                         f"{db.money(a['cash'] or 0)} · {db.money(a['debts'])}")
    return "\n".join(lines)
