from __future__ import annotations

import asyncpg

from .settings import Settings


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if not self.settings.database_url:
            return
        self.pool = await asyncpg.create_pool(self.settings.database_url, min_size=1, max_size=5)

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

    async def status(self) -> dict:
        if not self.settings.database_url:
            return {"configured": False, "connected": False, "message": "DATABASE_URL 필요"}
        if not self.pool:
            return {"configured": True, "connected": False, "message": "연결 대기"}
        try:
            row = await self.fetchrow("select postgis_full_version() as version")
            return {"configured": True, "connected": True, "postgis": row["version"]}
        except Exception as exc:
            return {"configured": True, "connected": False, "message": str(exc)}
