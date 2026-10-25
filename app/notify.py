"""Сообщения, которые бот рассылает сотрудникам. Используются и ботом, и API."""
from __future__ import annotations

import logging

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
