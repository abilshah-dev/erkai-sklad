"""Все обращения к базе PostgreSQL. Бот и API работают только через эти функции.

База стоит на нашем сервере. У каждого клиента своя база, адрес в DATABASE_URL.
"""
from __future__ import annotations  # чтобы код работал и на Python 3.9 (Mac), и на 3.12 (сервер)

import datetime as dt
import decimal
import json
import secrets
import uuid

import asyncpg

from . import config

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
    "NOT_ENOUGH_STOCK": "Не хватает товара.",
    "NOT_YOUR_CLIENT": "Это клиент другого агента.",
    "BAD_PAY": "Неверный способ оплаты.",
    "BAD_QTY": "Укажите количество.",
    "NOT_IN_CAR": "В машине нет столько товара.",
    "DEBT_STOP": "У клиента долг старше допустимого срока — продажа в долг заблокирована. Возьмите оплату сразу.",
    "DEBT_LIMIT": "Превышен лимит долга клиента.",
    "NO_DEBT": "У клиента нет долга.",
    "NO_CASH": "У вас нет наличных для сдачи.",
}


class DbError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


def money(x) -> str:
    try:
        return f"{round(float(x)):,}".replace(",", " ") + " с"
    except (TypeError, ValueError):
        return str(x)


def _human(msg: str) -> str:
    for code, text in ERRORS.items():
        if code in msg:
            parts = msg.split(":")
            if code == "LIMIT_EXCEEDED" and len(parts) >= 3:
                return f"Превышен лимит подотчёта {money(parts[-1])}: в машине уже {money(parts[-2])}."
            if code == "DEBT_LIMIT" and len(parts) >= 3:
                return f"Превышен лимит долга клиента {money(parts[-1])}: уже должен {money(parts[-2])}."
            if code == "DEBT_STOP" and len(parts) >= 2:
                return (f"Самому старому долгу клиента {parts[-1].strip()} дн. — продажа в долг заблокирована. "
                        "Возьмите оплату сразу.")
            return text
    return "Ошибка базы данных. Попробуйте ещё раз."


# ---------- Подключение ----------

_pool: asyncpg.Pool | None = None


async def _init_conn(conn):
    # JSON из базы сразу превращается в словари Python и обратно
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5, init=_init_conn)
    return _pool


def _clean(v):
    """Значения из базы → то, что можно отдать в JSON (uuid → текст, numeric → число, дата → ISO)."""
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


def _row(r) -> dict | None:
    return {k: _clean(v) for k, v in r.items()} if r is not None else None


async def fetch(sql: str, *args) -> list[dict]:
    try:
        async with (await pool()).acquire() as c:
            return [_row(r) for r in await c.fetch(sql, *args)]
    except asyncpg.PostgresError as e:
        raise DbError(_human(str(e))) from e


async def fetchrow(sql: str, *args) -> dict | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def fetchval(sql: str, *args):
    try:
        async with (await pool()).acquire() as c:
            return _clean(await c.fetchval(sql, *args))
    except asyncpg.PostgresError as e:
        raise DbError(_human(str(e))) from e


async def rpc(name: str, params: dict):
    """Вызов функции из schema.sql: rpc("create_request", {"p_agent": ..., ...})."""
    args = ", ".join(f"{k} => ${i + 1}" for i, k in enumerate(params))
    return await fetchval(f"select {name}({args})", *params.values())


# ---------- Сотрудники ----------

async def user_by_tg(tg_id: int):
    return await fetchrow("select * from users where tg_id = $1", tg_id)


async def user_by_id(uid: str):
    return await fetchrow("select * from users where id = $1", uid)


async def ensure_owner(tg_id: int, name: str):
    return await fetchrow(
        """insert into users(tg_id, full_name, role) values ($1, $2, 'owner')
           on conflict (tg_id) do update set role = 'owner', active = true returning *""", tg_id, name)


async def users_by_roles(roles: list[str], active_only: bool = True):
    sql = "select * from users where role = any($1::text[])"
    if active_only:
        sql += " and active"
    return await fetch(sql + " order by full_name", roles)


async def use_invite(code: str, tg_id: int):
    return await rpc("use_invite", {"p_code": code, "p_tg": tg_id})


async def create_invite(role: str, full_name: str, by: str) -> str:
    code = secrets.token_hex(4).upper()
    await fetch("insert into invites(code, role, full_name, created_by) values ($1, $2, $3, $4)",
                code, role, full_name, by)
    return code


# ---------- Товары и остатки ----------

async def catalog():
    return await fetch("select * from v_catalog order by group_name nulls last, name")


async def car(agent_id: str):
    return await fetch("select * from v_stock where agent_id = $1 order by product", agent_id)


async def all_cars():
    return await fetch("select * from v_stock where kind = 'agent'")


async def setting(key: str, default=None):
    v = await fetchval("select value from settings where key = $1", key)
    return default if v is None else v


# ---------- Заявки ----------

async def create_request(agent_id: str, kind: str, items: list[dict]):
    return await rpc("create_request", {"p_agent": agent_id, "p_kind": kind, "p_items": items})


async def confirm_request(request_id: str, user_id: str, items: list[dict] | None):
    return await rpc("confirm_request", {"p_request": request_id, "p_user": user_id, "p_items": items})


async def cancel_request(request_id: str, user_id: str):
    return await rpc("cancel_request", {"p_request": request_id, "p_user": user_id})


async def get_request(request_id: str):
    """Заявка со строками, названиями товаров и именем агента."""
    try:
        uuid.UUID(str(request_id))
    except ValueError:
        return None
    r = await fetchrow("""select r.*, u.full_name as agent_name from requests r
                          join users u on u.id = r.agent_id where r.id = $1""", request_id)
    if not r:
        return None
    items = await fetch("""select ri.id, ri.product_id, ri.qty_requested, ri.qty_actual,
                                  p.name, p.unit, p.price
                             from request_items ri join products p on p.id = ri.product_id
                            where ri.request_id = $1 order by p.name""", request_id)
    r["request_items"] = [{"id": i["id"], "product_id": i["product_id"], "qty_requested": i["qty_requested"],
                           "qty_actual": i["qty_actual"],
                           "products": {"name": i["name"], "unit": i["unit"], "price": i["price"]}} for i in items]
    return r


async def open_requests(agent_id: str | None = None):
    if agent_id:
        ids = await fetch("select id from requests where status = 'new' and agent_id = $1 order by created_at", agent_id)
    else:
        ids = await fetch("select id from requests where status = 'new' order by created_at")
    return [await get_request(r["id"]) for r in ids]


# ---------- Клиенты ----------

async def clients(agent_id: str | None = None):
    """Клиенты с текущим долгом и возрастом самого старого долга."""
    sql = """select c.*, coalesce(d.debt, 0) as debt, coalesce(d.oldest_days, 0) as oldest_days
               from clients c left join v_client_debts d on d.client_id = c.id"""
    if agent_id:
        return await fetch(sql + " where c.agent_id = $1 order by c.name", agent_id)
    return await fetch(sql + " order by c.name")


async def add_client(data: dict):
    return await fetchrow(
        """insert into clients(name, place, phone, agent_id, created_by)
           values ($1, $2, $3, $4, $5) returning *""",
        data["name"], data.get("place"), data.get("phone"), data.get("agent_id"), data.get("created_by"))


async def transfer_clients(from_id: str, to_id: str, by: str, block: bool) -> int:
    return await rpc("transfer_clients", {"p_from": from_id, "p_to": to_id, "p_by": by, "p_block": block})


# ---------- Журнал ----------

async def journal(limit: int = 50):
    return await fetch("select * from v_movements order by id desc limit $1", limit)


# ---------- Продажи (этап 2) ----------

async def create_sale(agent_id: str, client_id: str, items: list[dict], pay: str, paid: float):
    return await rpc("create_sale", {"p_agent": agent_id, "p_client": client_id, "p_items": items,
                                     "p_pay": pay, "p_paid": paid})


async def decide_sale(sale_id: str, user_id: str, approve: bool):
    return await rpc("decide_sale", {"p_sale": sale_id, "p_user": user_id, "p_approve": approve})


async def get_sale(sale_id: str):
    """Продажа с клиентом, агентом и строками — для накладной и уведомлений."""
    try:
        uuid.UUID(str(sale_id))
    except ValueError:
        return None
    s = await fetchrow("""select s.*, c.name as client_name, c.place as client_place, c.phone as client_phone,
                                 u.full_name as agent_name
                            from sales s join clients c on c.id = s.client_id join users u on u.id = s.agent_id
                           where s.id = $1""", sale_id)
    if not s:
        return None
    s["items"] = await fetch("""select si.qty, si.price, si.discount_pct, si.amount, p.name, p.unit,
                                       coalesce(g.max_discount, 0) as max_discount
                                  from sale_items si join products p on p.id = si.product_id
                                  left join product_groups g on g.id = p.group_id
                                 where si.sale_id = $1 order by p.name""", sale_id)
    s["debt_after"] = await fetchval("select coalesce(sum(remaining), 0) from debts where client_id = $1",
                                     s["client_id"])
    return s


async def pending_sales():
    ids = await fetch("select id from sales where status = 'pending' order by created_at")
    return [await get_sale(r["id"]) for r in ids]


async def take_payment(user_id: str, client_id: str, amount: float, method: str):
    return await rpc("take_payment", {"p_user": user_id, "p_client": client_id, "p_amount": amount,
                                      "p_method": method})


# ---------- Деньги агентов ----------

async def agent_cash(agent_id: str):
    return await fetchrow("select * from v_agent_cash where agent_id = $1", agent_id)


async def all_agent_cash():
    return await fetch("select * from v_agent_cash order by full_name")


async def create_handover(agent_id: str):
    return await rpc("create_handover", {"p_agent": agent_id})


async def decide_handover(hid: str, user_id: str, ok: bool):
    return await rpc("decide_handover", {"p_id": hid, "p_user": user_id, "p_ok": ok})


async def pending_handovers():
    return await fetch("""select h.*, u.full_name as agent_name from handovers h
                          join users u on u.id = h.agent_id where h.status = 'new' order by h.created_at""")


# ---------- Сводка и настройки ----------

async def owner_summary():
    return await fetchval("select owner_summary()")


SETTING_KEYS = ("agent_stock_limit", "client_debt_limit", "debt_stop_days")


async def get_settings():
    rows = await fetch("select key, value from settings where key = any($1::text[])", list(SETTING_KEYS))
    groups = await fetch("select id, name, max_discount from product_groups order by name")
    return {"settings": {r["key"]: r["value"] for r in rows}, "groups": groups}


async def save_settings(values: dict, groups: dict, by: str):
    for k, v in values.items():
        if k in SETTING_KEYS:
            await fetch("update settings set value = $2 where key = $1", k, max(0, float(v)))
    for gid, pct in groups.items():
        await fetch("update product_groups set max_discount = $2 where id = $1", gid, min(100, max(0, float(pct))))
    await fetch("insert into audit_log(user_id, action, details) values ($1, 'settings_changed', $2)",
                by, {"settings": values, "groups": groups})


async def client_by_id(cid: str):
    try:
        uuid.UUID(str(cid))
    except ValueError:
        return None
    return await fetchrow("select * from clients where id = $1", cid)
