"""Сквозной тест на настоящей базе PostgreSQL: API + уведомления бота.

Нужна пустая тестовая база. Запуск:
  TEST_DATABASE_URL=postgresql://postgres@localhost/sklad_test .venv/bin/python -m tests.test_e2e
Тест сам создаёт схему, поэтому база должна быть пустой (тест её пересоздаёт).
"""
import asyncio
import os
from pathlib import Path

import asyncpg

os.environ.update(BOT_TOKEN="123:TEST", DATABASE_URL=os.environ["TEST_DATABASE_URL"],
                  WEBAPP_URL="https://sklad.erkai.kg/t", ENV_FILE="/nonexistent")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from app import api, db  # noqa: E402
from tests.test_api import init_data  # noqa: E402  (подписанные данные Telegram)

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


class FakeBot:
    """Вместо Telegram: запоминаем, кому что бот отправил бы."""
    username = "test_bot"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def send_document(self, chat_id, document, caption="", **kw):
        data = document.input_file_content
        assert data[:4] == b"%PDF", "накладная должна быть PDF"
        self.sent.append((chat_id, "PDF " + caption))
        self.pdf = data


async def reset_db():
    conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    await conn.execute("drop schema public cascade; create schema public;")
    await conn.execute(SCHEMA.read_text())
    await conn.close()
    from tools import migrate   # применяем миграции, как на сервере
    await migrate.main()


async def main():
    await reset_db()
    # Сотрудники и товары
    await db.ensure_owner(1, "Владелец")
    for tg, name, role in [(2, "Складовщик", "storekeeper"), (3, "Бакыт", "agent"),
                           (4, "Нурлан", "agent"), (5, "Бухгалтер", "accountant")]:
        code = await db.create_invite(role, name, None)
        await db.use_invite(code.lower(), tg)
    await db.rpc("import_catalog", {"p_rows": [
        {"code": "001", "name": "Цемент М400", "group": "Цемент", "unit": "меш.", "price": 420, "stock": 320},
        {"code": "002", "name": "Гипсокартон", "group": "Листовые", "unit": "лист", "price": 380, "stock": 50}]})

    bot = FakeBot()
    client = TestClient(TestServer(api.make_app(bot)))
    await client.start_server()
    H = lambda tg: {"X-Init-Data": init_data(tg)}  # noqa: E731

    async def call(method, path, tg, body=None, status=200):
        r = await getattr(client, method)(path, headers=H(tg), json=body)
        data = await r.json()
        assert r.status == status, (path, r.status, data)
        return data

    try:
        cat = await call("get", "/api/catalog", 3)
        cement = next(p for p in cat if p["name"] == "Цемент М400")
        gk = next(p for p in cat if p["name"] == "Гипсокартон")

        # 1. Агент заказывает 100 цемента и 60 гипсокартона (на складе только 50)
        req = await call("post", "/api/requests", 3, {"kind": "issue", "items": [
            {"product_id": cement["id"], "qty": 100}, {"product_id": gk["id"], "qty": 60}]})
        assert req["no"].startswith("№")
        assert any(t == 2 and "Заявка на выдачу" in x for t, x in bot.sent), "складовщику пришло уведомление"

        # 2. Складовщик открывает заявку и выдаёт 80 цемента вместо 100
        r = await call("get", f"/api/requests/{req['id']}", 2)
        cem_item = next(i for i in r["request_items"] if i["products"]["name"] == "Цемент М400")
        await call("post", f"/api/requests/{req['id']}/confirm", 2,
                   {"items": [{"item_id": cem_item["id"], "qty_actual": 80}]})
        assert any(t == 3 and "выдано" in x for t, x in bot.sent), "агенту пришло подтверждение"
        assert any(t == 5 and "Бакыт" in x for t, x in bot.sent), "бухгалтеру пришла копия"

        # 3. В машине: 80 цемента и 50 гипсокартона (больше на складе не было)
        car = {i["product"]: i["qty"] for i in (await call("get", "/api/car", 3))["items"]}
        assert car == {"Цемент М400": 80, "Гипсокартон": 50}, car

        # 4. Повторное подтверждение запрещено
        await call("post", f"/api/requests/{req['id']}/confirm", 2, {}, status=400)

        # 5. Лимит подотчёта 300 000 с: в машине уже 52 600, просим ещё 600 мешков = 252 000 → откажет
        err = await call("post", "/api/requests", 3, {"items": [{"product_id": cement["id"], "qty": 600}]}, status=400)
        assert "лимит" in err["error"].lower(), err

        # 6. Возврат 30 мешков
        ret = await call("post", "/api/requests", 3, {"kind": "return", "items": [{"product_id": cement["id"], "qty": 30}]})
        assert ret["no"].startswith("№")
        await call("post", f"/api/requests/{ret['id']}/confirm", 2, {})
        stock = {p["name"]: p["stock"] for p in await call("get", "/api/catalog", 2)}
        assert stock["Цемент М400"] == 320 - 80 + 30, stock

        # 7. Клиенты: агент добавляет, бухгалтер видит всех и передаёт другому агенту с отключением
        await call("post", "/api/clients", 3, {"name": "Строй-Дом", "place": "Аламедин", "phone": "+996 555"})
        await call("post", "/api/clients", 4, {"name": "Уют"})
        assert len(await call("get", "/api/clients", 3)) == 1, "агент видит только своих"
        assert len(await call("get", "/api/clients", 5)) == 2, "бухгалтер видит всех"
        agents = {a["full_name"]: a for a in await call("get", "/api/agents", 5)}
        assert agents["Бакыт"]["car_value"] == 50 * 420 + 50 * 380, agents["Бакыт"]
        moved = await call("post", "/api/clients/transfer", 5,
                           {"from": agents["Бакыт"]["id"], "to": agents["Нурлан"]["id"], "block": True})
        assert moved["moved"] == 1
        await call("get", "/api/me", 3, status=403)  # уволенный агент больше не входит
        assert len(await call("get", "/api/clients", 4)) == 2

        # 8. Журнал
        j = await call("get", "/api/journal", 5)
        assert [m["doc_type"] for m in j][:3] == ["return", "issue", "issue"], [m["doc_type"] for m in j]

        # ---------- Этап 2 ----------
        # 9. Новый агент Азамат, получает товар; владелец ставит лимиты скидок
        code = await db.create_invite("agent", "Азамат", None)
        await db.use_invite(code, 6)
        r = await call("post", "/api/requests", 6, {"items": [{"product_id": cement["id"], "qty": 50},
                                                               {"product_id": gk["id"], "qty": 0}]})
        assert r["no"].startswith("№"), r
        await call("post", f"/api/requests/{r['id']}/confirm", 2, {})
        st = await call("get", "/api/settings", 1)
        groups = {g["name"]: g["id"] for g in st["groups"]}
        await call("post", "/api/settings", 1, {"settings": {"client_debt_limit": 16000, "debt_stop_days": 30},
                                                "groups": {groups["Цемент"]: 5}})
        await call("get", "/api/settings", 5, status=403)  # настройки только у владельца
        cl = await call("post", "/api/clients", 6, {"name": "Евроремонт"})

        # 10. Продажа 10 мешков со скидкой 3% (в лимите), частично: 1000 сразу
        sale = await call("post", "/api/sales", 6, {"client_id": cl["id"], "pay": "part", "paid": 1000,
                                                    "items": [{"product_id": cement["id"], "qty": 10, "discount_pct": 3}]})
        assert sale["status"] == "done" and sale["total"] == 4074 and sale["paid"] == 1000, sale
        assert any(t == 6 and x.startswith("PDF") for t, x in bot.sent), "агент получил PDF"
        assert any(t == 5 and "Продажа №" in x for t, x in bot.sent), "бухгалтер получил копию"

        # 11. Скидка 10% — к владельцу; одобрение проводит продажу
        p2 = await call("post", "/api/sales", 6, {"client_id": cl["id"], "pay": "cash",
                                                  "items": [{"product_id": cement["id"], "qty": 5, "discount_pct": 10}]})
        assert p2["status"] == "pending"
        assert any(t == 1 and "Запрос на скидку" in x for t, x in bot.sent)
        await call("post", f"/api/sales/{p2['id']}/decide", 5, {"approve": True}, status=403)  # бухгалтер не одобряет
        await call("post", f"/api/sales/{p2['id']}/decide", 1, {"approve": True})
        car = {i["product"]: i["qty"] for i in (await call("get", "/api/car", 6))["items"]}
        assert car["Цемент М400"] == 35, car

        # 12. Лимит долга 16 000: долг 3 074, продажа 30 мешков (12 600) в долг — ок, ещё 5 000 — откажет
        await call("post", "/api/sales", 6, {"client_id": cl["id"], "pay": "debt",
                                             "items": [{"product_id": cement["id"], "qty": 30}]})
        e = await call("post", "/api/sales", 6, {"client_id": cl["id"], "pay": "debt",
                                                 "items": [{"product_id": cement["id"], "qty": 5}]}, status=400)
        assert "лимит долга" in e["error"].lower(), e

        # 13. Оплата долга, сдача денег, подтверждение бухгалтером
        clients = {c["name"]: c for c in await call("get", "/api/clients", 6)}
        assert clients["Евроремонт"]["debt"] == 3074 + 12600, clients["Евроремонт"]
        pay = await call("post", "/api/payments", 6, {"client_id": cl["id"], "amount": 5000, "method": "cash"})
        assert pay["paid"] == 5000 and pay["debt_left"] == 10674, pay
        money = await call("get", "/api/car", 6)
        assert money["cash_on_hand"] == 1000 + 1890 + 5000, money   # частичная + одобренная наличными + оплата долга
        ho = await call("post", "/api/handovers", 6, {})
        assert any(t == 5 and "сдаёт наличные" in x for t, x in bot.sent)
        await call("post", f"/api/handovers/{ho['id']}/decide", 5, {"ok": True})
        assert (await call("get", "/api/car", 6))["cash_on_hand"] == 0

        # 14. Сводка владельца
        s = await call("get", "/api/summary", 1)
        assert s["sales_count"] == 3 and s["sales_today"] == 4074 + 1890 + 12600, s
        assert s["debts_total"] == 10674 and s["cash_at_agents"] == 0, s
        from app import notify
        assert "Продажи сегодня" in await notify.summary_text()
        open(os.environ.get("PDF_OUT", "/dev/null"), "wb").write(bot.pdf)
    finally:
        await client.close()
    print("Сквозной тест пройден: этап 1 (склад) и этап 2 (продажи, скидки, долги, касса, сводка)")


asyncio.run(main())
