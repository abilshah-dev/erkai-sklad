"""Все обращения к базе Supabase. Бот и API работают только через эти функции."""
from __future__ import annotations  # чтобы код работал и на Python 3.9 (Mac), и на 3.12 (сервер)

import asyncio
import secrets

from postgrest.exceptions import APIError
from supabase import create_client

from . import config

sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

ROLE_NAMES = {
    "owner": "Владелец",
    "accountant": "Бухгалтер",
    "storekeeper": "Складовщик",
    "agent": "Торговый агент",
}

# Понятные тексты для ошибок, которые выбрасывает база (raise exception в schema.sql)
ERRORS = {
    "LIMIT_EXCEEDED": "Превышен лимит подотчёта. Сдайте отчёт или уменьшите заявку.",
    "RETURN_MORE_THAN_CAR": "Нельзя вернуть больше, чем есть в машине.",
    "ALREADY_DONE": "Заявка уже обработана.",
    "NOT_FOUND": "Заявка не найдена.",
    "FORBIDDEN": "Нет прав на это действие.",
    "BAD_INVITE": "Код приглашения не найден или уже использован.",
    "EMPTY": "Заявка пустая.",
    "NOT_AGENT": "Заявки создают только активные торговые агенты.",
    "SAME_AGENT": "Выберите другого агента.",
    "TARGET_NOT_AGENT": "Получатель должен быть активным агентом.",
    "CANNOT_CANCEL": "Эту заявку уже нельзя отменить.",
    "NOT_ENOUGH_STOCK": "Не хватает товара на складе.",
}


class DbError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


def _human(e: APIError) -> str:
    msg = str(getattr(e, "message", "") or e)
    for code, text in ERRORS.items():
        if code in msg:
            if code == "LIMIT_EXCEEDED":
                parts = msg.split(":")
                if len(parts) >= 3:
                    return f"Превышен лимит подотчёта {money(parts[-1])}: в машине уже {money(parts[-2])}."
            return text
    return "Ошибка базы данных. Попробуйте ещё раз."


async def run(fn):
    """Клиент Supabase синхронный — выполняем запрос в отдельном потоке, чтобы бот не зависал."""
    try:
        return await asyncio.to_thread(fn)
    except APIError as e:
        raise DbError(_human(e)) from e


async def rpc(name: str, params: dict):
    res = await run(lambda: sb.rpc(name, params).execute())
    return res.data


def money(x) -> str:
    try:
        return f"{round(float(x)):,}".replace(",", " ") + " с"
    except (TypeError, ValueError):
        return str(x)


# ---------- Сотрудники ----------

async def user_by_tg(tg_id: int):
    res = await run(lambda: sb.table("users").select("*").eq("tg_id", tg_id).limit(1).execute())
    return res.data[0] if res.data else None


async def user_by_id(uid: str):
    res = await run(lambda: sb.table("users").select("*").eq("id", uid).limit(1).execute())
    return res.data[0] if res.data else None


async def ensure_owner(tg_id: int, name: str):
    res = await run(lambda: sb.table("users").upsert(
        {"tg_id": tg_id, "full_name": name, "role": "owner", "active": True}, on_conflict="tg_id").execute())
    return res.data[0]


async def users_by_roles(roles: list[str], active_only: bool = True):
    def q():
        query = sb.table("users").select("*").in_("role", roles)
        if active_only:
            query = query.eq("active", True)
        return query.order("full_name").execute()
    return (await run(q)).data


async def use_invite(code: str, tg_id: int):
    return await rpc("use_invite", {"p_code": code, "p_tg": tg_id})


async def create_invite(role: str, full_name: str, by: str) -> str:
    code = secrets.token_hex(4).upper()
    await run(lambda: sb.table("invites").insert(
        {"code": code, "role": role, "full_name": full_name, "created_by": by}).execute())
    return code


# ---------- Товары и остатки ----------

async def catalog():
    return (await run(lambda: sb.table("v_catalog").select("*").order("group_name").order("name").execute())).data


async def car(agent_id: str):
    rows = (await run(lambda: sb.table("v_stock").select("*").eq("agent_id", agent_id).order("product").execute())).data
    return rows


async def all_cars():
    return (await run(lambda: sb.table("v_stock").select("*").eq("kind", "agent").execute())).data


async def setting(key: str, default=None):
    res = await run(lambda: sb.table("settings").select("value").eq("key", key).limit(1).execute())
    return res.data[0]["value"] if res.data else default


# ---------- Заявки ----------

async def create_request(agent_id: str, kind: str, items: list[dict]):
    return await rpc("create_request", {"p_agent": agent_id, "p_kind": kind, "p_items": items})


async def confirm_request(request_id: str, user_id: str, items: list[dict] | None):
    return await rpc("confirm_request", {"p_request": request_id, "p_user": user_id, "p_items": items})


async def cancel_request(request_id: str, user_id: str):
    return await rpc("cancel_request", {"p_request": request_id, "p_user": user_id})


async def get_request(request_id: str):
    """Заявка со строками, названиями товаров и именем агента."""
    res = await run(lambda: sb.table("requests")
                    .select("*, request_items(id, product_id, qty_requested, qty_actual, products(name, unit, price))")
                    .eq("id", request_id).limit(1).execute())
    if not res.data:
        return None
    r = res.data[0]
    agent = await user_by_id(r["agent_id"])
    r["agent_name"] = agent["full_name"] if agent else "?"
    return r


async def open_requests(agent_id: str | None = None):
    def q():
        query = sb.table("requests").select("id").eq("status", "new").order("created_at")
        if agent_id:
            query = query.eq("agent_id", agent_id)
        return query.execute()
    ids = [r["id"] for r in (await run(q)).data]
    return [await get_request(i) for i in ids]


# ---------- Клиенты ----------

async def clients(agent_id: str | None = None):
    def q():
        query = sb.table("clients").select("*").order("name")
        if agent_id:
            query = query.eq("agent_id", agent_id)
        return query.execute()
    return (await run(q)).data


async def add_client(data: dict):
    return (await run(lambda: sb.table("clients").insert(data).execute())).data[0]


async def transfer_clients(from_id: str, to_id: str, by: str, block: bool) -> int:
    return await rpc("transfer_clients", {"p_from": from_id, "p_to": to_id, "p_by": by, "p_block": block})


# ---------- Журнал ----------

async def journal(limit: int = 50):
    return (await run(lambda: sb.table("v_movements").select("*").order("id", desc=True).limit(limit).execute())).data
