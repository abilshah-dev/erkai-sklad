"""Тесты API без настоящей базы: функции db подменяются данными в памяти.

Запуск:  .venv/bin/python -m tests.test_api
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

os.environ.update(BOT_TOKEN="123:TEST", SUPABASE_URL="https://x.supabase.co",
                  SUPABASE_SERVICE_KEY="eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.x",
                  WEBAPP_URL="https://sklad.erkai.kg/t", ENV_FILE="/nonexistent")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from app import api, auth, db, notify  # noqa: E402


def init_data(tg_id, token="123:TEST", age=0):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAA",
              "user": json.dumps({"id": tg_id, "first_name": "Тест"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


USERS = {
    1: {"id": "u-owner", "tg_id": 1, "full_name": "Владелец", "role": "owner", "active": True},
    2: {"id": "u-store", "tg_id": 2, "full_name": "Склад", "role": "storekeeper", "active": True},
    3: {"id": "u-agent", "tg_id": 3, "full_name": "Бакыт", "role": "agent", "active": True},
    4: {"id": "u-off", "tg_id": 4, "full_name": "Уволен", "role": "agent", "active": False},
}
calls, sent = [], []


async def fake_user_by_tg(tg):
    return USERS.get(tg)


async def fake_create_request(agent, kind, items):
    calls.append(("create", agent, kind, items))
    if sum(i["qty"] for i in items) > 1000:
        raise db.DbError("Превышен лимит подотчёта")
    return {"id": "r1", "no": "З-00001", "kind": kind}


async def fake_confirm(rid, uid, items):
    calls.append(("confirm", rid, uid, items))
    return {"id": rid, "no": "З-00001", "kind": "issue", "agent_id": "u-agent", "short": 0}


async def fake_notify_new(bot, rid):
    sent.append(("new", rid))


async def fake_notify_confirmed(bot, rid, name):
    sent.append(("confirmed", rid, name))


async def fake_setting(key, default=None):
    return 300000


async def fake_catalog():
    return [{"id": "p1", "name": "Цемент", "unit": "меш.", "price": 420, "stock": 10, "group_name": "Цемент"}]


db.user_by_tg = fake_user_by_tg
db.create_request = fake_create_request
db.confirm_request = fake_confirm
db.setting = fake_setting
db.catalog = fake_catalog
notify.new_request = fake_notify_new
notify.confirmed = fake_notify_confirmed


async def main():
    # Подпись Telegram
    assert auth.validate_init_data(init_data(3), "123:TEST")["id"] == 3
    assert auth.validate_init_data(init_data(3), "999:OTHER") is None, "чужой токен должен отклоняться"
    assert auth.validate_init_data(init_data(3, age=90000), "123:TEST") is None, "старые данные отклоняются"
    assert auth.validate_init_data(init_data(3).replace("query_id=AAA", "query_id=BBB"), "123:TEST") is None

    client = TestClient(TestServer(api.make_app(bot=None)))
    await client.start_server()
    H = lambda tg: {"X-Init-Data": init_data(tg)}  # noqa: E731
    try:
        r = await client.get("/api/me")
        assert r.status == 401, r.status
        r = await client.get("/api/me", headers=H(4))
        assert r.status == 403, "отключённый агент не должен входить"
        r = await client.get("/api/me", headers=H(3))
        me = await r.json()
        assert me["role"] == "agent" and me["agent_limit"] == 300000, me

        r = await client.get("/api/catalog", headers=H(3))
        assert (await r.json())[0]["name"] == "Цемент"

        r = await client.post("/api/requests", headers=H(3), json={"kind": "issue", "items": [
            {"product_id": "p1", "qty": 5}, {"product_id": "p2", "qty": 0}]})
        assert r.status == 200 and (await r.json())["no"] == "З-00001"
        assert calls[-1] == ("create", "u-agent", "issue", [{"product_id": "p1", "qty": 5}]), calls[-1]
        assert sent[-1] == ("new", "r1")

        r = await client.post("/api/requests", headers=H(3), json={"items": [{"product_id": "p1", "qty": 5000}]})
        assert r.status == 400 and "лимит" in (await r.json())["error"]

        r = await client.post("/api/requests", headers=H(2), json={"items": [{"product_id": "p1", "qty": 1}]})
        assert r.status == 403, "складовщик не создаёт заявки"

        r = await client.post("/api/requests/r1/confirm", headers=H(3), json={})
        assert r.status == 403, "агент не может сам себе подтвердить выдачу"

        r = await client.post("/api/requests/r1/confirm", headers=H(2),
                              json={"items": [{"item_id": "i1", "qty_actual": 3}]})
        assert r.status == 200
        assert calls[-1] == ("confirm", "r1", "u-store", [{"item_id": "i1", "qty_actual": 3}])
        assert sent[-1] == ("confirmed", "r1", "Склад")

        r = await client.post("/api/clients/transfer", headers=H(3), json={"from": "a", "to": "b"})
        assert r.status == 403, "агент не передаёт клиентов"

        r = await client.get("/", headers={})
        assert r.status == 200 and "Склад" in await r.text()
    finally:
        await client.close()
    print("Все проверки пройдены")


asyncio.run(main())
