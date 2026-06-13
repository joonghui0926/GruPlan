from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from .data_catalog import PUBLIC_DATA_SOURCES
from .settings import Settings


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if not self.settings.database_url:
            return
        self.pool = await asyncpg.create_pool(self.settings.database_url, min_size=1, max_size=5)
        await self.apply_schema()
        await self.seed_catalog()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def fetch(self, query: str, *args):
        if not self.pool:
            raise RuntimeError("DATABASE_URL is not configured")
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        if not self.pool:
            raise RuntimeError("DATABASE_URL is not configured")
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def execute(self, query: str, *args) -> str:
        if not self.pool:
            raise RuntimeError("DATABASE_URL is not configured")
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def apply_schema(self) -> None:
        if not self.pool:
            return
        schema_path = Path(__file__).resolve().parent / "sql" / "schema.sql"
        statements = [statement.strip() for statement in schema_path.read_text(encoding="utf-8").split(";") if statement.strip()]
        async with self.pool.acquire() as conn:
            for statement in statements:
                await conn.execute(statement)

    async def seed_catalog(self) -> None:
        if not self.pool:
            return
        now = datetime.now(timezone.utc)
        rows = [
            (
                source.id,
                source.name,
                source.provider,
                source.kind,
                source.access,
                source.url,
                source.usage,
                source.table_name,
                source.requires_key,
                source.ingestion,
                source.license_note,
                now,
            )
            for source in PUBLIC_DATA_SOURCES
        ]
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                insert into public_data_sources (
                  id, name, provider, kind, access, url, usage, table_name,
                  requires_key, ingestion, license_note, last_checked_at
                )
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                on conflict (id) do update set
                  name = excluded.name,
                  provider = excluded.provider,
                  kind = excluded.kind,
                  access = excluded.access,
                  url = excluded.url,
                  usage = excluded.usage,
                  table_name = excluded.table_name,
                  requires_key = excluded.requires_key,
                  ingestion = excluded.ingestion,
                  license_note = excluded.license_note,
                  last_checked_at = excluded.last_checked_at
                """,
                rows,
            )

    async def status(self) -> dict:
        if not self.settings.database_url:
            return {"configured": False, "connected": False, "message": "DATABASE_URL 필요"}
        if not self.pool:
            return {"configured": True, "connected": False, "message": "연결 대기"}
        try:
            row = await self.fetchrow("select postgis_full_version() as version")
            catalog = await self.fetchrow("select count(*) as total from public_data_sources")
            return {"configured": True, "connected": True, "postgis": row["version"], "catalog": int(catalog["total"])}
        except Exception as exc:
            return {"configured": True, "connected": False, "message": str(exc)}
