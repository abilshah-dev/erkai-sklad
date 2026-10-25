"""HTTP-сервер: отдаёт окно Mini App (webapp/index.html) и API для него."""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web
from telegram import Bot

from . import config, db, notify
from .auth import validate_init_data

log = logging.getLogger(__name__)
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
STAFF = ("owner", "accountant")


def err(status: int, text: str):
    return web.json_response({"error": text}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Каждый запрос к /api/ должен нести подписанные данные Telegram."""
    if request.path.startswith("/api/"):
        tg = validate_init_data(request.headers.get("X-Init-Data", ""), config.BOT_TOKEN)
        if not tg:
            return err(401, "Откройте окно из бота в Telegram.")
        user = await db.user_by_tg(int(tg["id"]))
        if not user or not user["active"]:
            return err(403, "Нет доступа. Обратитесь к бухгалтеру.")
        request["user"] = user
    try:
        return await handler(request)
    except db.DbError as e:
        return err(400, str(e))


def need(request: web.Request, *roles: str):
    if request["user"]["role"] not in roles:
        raise web.HTTPForbidden(text='{"error":"Нет прав на это действие."}', content_type="application/json")


routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/api/me")
async def me(request):
    u = request["user"]
    limit = await db.setting("agent_stock_limit", 0)
    stop = await db.setting("debt_stop_days", 30)
    return web.json_response({"id": u["id"], "name": u["full_name"], "role": u["role"],
                              "company": config.COMPANY_NAME, "agent_limit": limit, "debt_stop_days": stop})


@routes.get("/api/catalog")
async def catalog(request):
    return web.json_response(await db.catalog())


@routes.get("/api/car")
async def car(request):
    u = request["user"]
    agent_id = u["id"]
    if u["role"] in STAFF and request.query.get("agent"):
        agent_id = request.query["agent"]
    else:
        need(request, "agent")
    rows = await db.car(agent_id)
    pending = await db.open_requests(agent_id)
    cash = await db.agent_cash(agent_id) or {}
    debts = sum(float(c["debt"]) for c in await db.clients(agent_id))
    return web.json_response({"items": rows, "pending": [{"no": r["no"], "kind": r["kind"]} for r in pending],
                              "cash_on_hand": cash.get("cash_on_hand", 0),
                              "pending_handover": cash.get("pending_handover", 0), "clients_debt": debts})


@routes.post("/api/requests")
async def create_request(request):
    need(request, "agent")
    body = await request.json()
    kind = body.get("kind", "issue")
    items = [{"product_id": i["product_id"], "qty": int(i["qty"])}
             for i in body.get("items", []) if int(i.get("qty", 0)) > 0]
    if not items:
        return err(400, "Заявка пустая.")
    res = await db.create_request(request["user"]["id"], kind, items)
    await notify.new_request(request.app["bot"], res["id"])
    return web.json_response(res)


@routes.get("/api/requests")
async def list_requests(request):
    need(request, "storekeeper", *STAFF)
    return web.json_response(await db.open_requests())


@routes.get("/api/requests/{rid}")
async def get_request(request):
    u = request["user"]
    r = await db.get_request(request.match_info["rid"])
    if not r:
        return err(404, "Заявка не найдена.")
    if u["role"] == "agent" and r["agent_id"] != u["id"]:
        return err(403, "Нет доступа.")
    return web.json_response(r)


@routes.post("/api/requests/{rid}/confirm")
async def confirm(request):
    need(request, "storekeeper", *STAFF)
    body = await request.json()
    items = [{"item_id": i["item_id"], "qty_actual": int(i["qty_actual"])} for i in body.get("items", [])]
    res = await db.confirm_request(request.match_info["rid"], request["user"]["id"], items or None)
    await notify.confirmed(request.app["bot"], res["id"], request["user"]["full_name"])
    return web.json_response(res)


@routes.post("/api/requests/{rid}/cancel")
async def cancel(request):
    await db.cancel_request(request.match_info["rid"], request["user"]["id"])
    return web.json_response({"ok": True})


@routes.get("/api/clients")
async def clients(request):
    u = request["user"]
    if u["role"] == "agent":
        return web.json_response(await db.clients(u["id"]))
    need(request, *STAFF)
    return web.json_response(await db.clients())


@routes.post("/api/clients")
async def add_client(request):
    u = request["user"]
    need(request, "agent", *STAFF)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return err(400, "Укажите название клиента.")
    agent_id = u["id"] if u["role"] == "agent" else body.get("agent_id")
    row = await db.add_client({"name": name, "place": body.get("place") or None, "phone": body.get("phone") or None,
                               "agent_id": agent_id, "created_by": u["id"]})
    return web.json_response(row)


@routes.post("/api/clients/transfer")
async def transfer(request):
    need(request, *STAFF)
    body = await request.json()
    n = await db.transfer_clients(body["from"], body["to"], request["user"]["id"], bool(body.get("block")))
    return web.json_response({"moved": n})


@routes.get("/api/agents")
async def agents(request):
    need(request, *STAFF)
    agents = await db.users_by_roles(["agent"], active_only=False)
    stock = await db.all_cars()
    for a in agents:
        a["car_value"] = sum(float(s["value"]) for s in stock if s["agent_id"] == a["id"])
    return web.json_response([{k: a[k] for k in ("id", "full_name", "active", "car_value")} for a in agents])


@routes.get("/api/journal")
async def journal(request):
    need(request, "storekeeper", *STAFF)
    return web.json_response(await db.journal(int(request.query.get("limit", 100))))


# ---------- Этап 2: продажи, оплаты, деньги, сводка, настройки ----------

@routes.post("/api/sales")
async def create_sale(request):
    need(request, "agent")
    body = await request.json()
    merged: dict[str, dict] = {}  # одна строка на товар
    for i in body.get("items", []):
        qty = int(i.get("qty", 0))
        if qty <= 0:
            continue
        m = merged.setdefault(i["product_id"], {"product_id": i["product_id"], "qty": 0, "discount_pct": 0})
        m["qty"] += qty
        m["discount_pct"] = max(float(m["discount_pct"]), float(i.get("discount_pct") or 0))
    if not merged:
        return err(400, "Добавьте товары.")
    pay = body.get("pay", "cash")
    res = await db.create_sale(request["user"]["id"], body.get("client_id"), list(merged.values()), pay,
                               float(body.get("paid") or 0))
    bot = request.app["bot"]
    if res["status"] == "done":
        await notify.sale_done(bot, res["id"])
    else:
        await notify.sale_pending(bot, res["id"])
    return web.json_response(res)


@routes.get("/api/approvals")
async def approvals(request):
    need(request, "owner")
    return web.json_response(await db.pending_sales())


@routes.post("/api/sales/{sid}/decide")
async def decide_sale(request):
    need(request, "owner")
    body = await request.json()
    res = await db.decide_sale(request.match_info["sid"], request["user"]["id"], bool(body.get("approve")))
    await notify.sale_decided(request.app["bot"], res, request["user"]["full_name"])
    return web.json_response(res)


@routes.post("/api/payments")
async def payment(request):
    need(request, "agent", *STAFF)
    body = await request.json()
    client = await db.client_by_id(body.get("client_id"))
    if not client:
        return err(404, "Клиент не найден.")
    method = body.get("method", "cash")
    res = await db.take_payment(request["user"]["id"], client["id"], float(body.get("amount") or 0), method)
    await notify.payment_taken(request.app["bot"], request["user"], client, res, method)
    return web.json_response(res)


@routes.post("/api/handovers")
async def create_handover(request):
    need(request, "agent")
    res = await db.create_handover(request["user"]["id"])
    await notify.handover_new(request.app["bot"], res, request["user"]["full_name"])
    return web.json_response(res)


@routes.get("/api/handovers")
async def handovers(request):
    need(request, *STAFF)
    return web.json_response({"pending": await db.pending_handovers(), "agents": await db.all_agent_cash()})


@routes.post("/api/handovers/{hid}/decide")
async def decide_handover(request):
    need(request, *STAFF)
    body = await request.json()
    res = await db.decide_handover(request.match_info["hid"], request["user"]["id"], bool(body.get("ok")))
    await notify.handover_decided(request.app["bot"], res, request["user"]["full_name"])
    return web.json_response(res)


@routes.get("/api/summary")
async def summary(request):
    need(request, *STAFF)
    return web.json_response(await db.owner_summary())


@routes.get("/api/settings")
async def get_settings(request):
    need(request, "owner")
    return web.json_response(await db.get_settings())


@routes.post("/api/settings")
async def save_settings(request):
    need(request, "owner")
    body = await request.json()
    await db.save_settings(body.get("settings", {}), body.get("groups", {}), request["user"]["id"])
    return web.json_response(await db.get_settings())


def make_app(bot: Bot) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["bot"] = bot
    app.add_routes(routes)
    return app
