"""Применяет новые миграции из db/migrations к базе клиента.

  ENV_FILE=/opt/erkai-sklad/clients/client1/.env .venv/bin/python -m tools.migrate

Каждая миграция применяется один раз (список применённых — в таблице schema_migrations)
и целиком в одной транзакции: если что-то упало, база остаётся как была.
"""
import asyncio
from pathlib import Path

import asyncpg

from app import config

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"


async def main():
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        await conn.execute("""create table if not exists schema_migrations (
                                name text primary key, applied_at timestamptz not null default now())""")
        done = {r["name"] for r in await conn.fetch("select name from schema_migrations")}
        files = sorted(MIGRATIONS.glob("*.sql"))
        new = [f for f in files if f.name not in done]
        if not new:
            print("Новых миграций нет.")
        for f in new:
            async with conn.transaction():
                await conn.execute(f.read_text(encoding="utf-8"))
                await conn.execute("insert into schema_migrations(name) values ($1)", f.name)
            print(f"Применена: {f.name}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
