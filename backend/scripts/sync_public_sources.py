from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import asyncpg
import httpx

from app.data_catalog import PUBLIC_DATA_SOURCES


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sync official public data source metadata.")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()

    conn = await asyncpg.connect(args.database_url)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for source in PUBLIC_DATA_SOURCES:
                status = None
                error = None
                try:
                    response = await client.get(source.url)
                    status = response.status_code
                    response.raise_for_status()
                except Exception as exc:
                    error = str(exc)
                await conn.execute(
                    """
                    insert into public_data_sources (
                      id, name, provider, kind, access, url, usage, table_name,
                      requires_key, ingestion, license_note, last_checked_at,
                      last_http_status, last_error
                    )
                    values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
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
                      last_checked_at = excluded.last_checked_at,
                      last_http_status = excluded.last_http_status,
                      last_error = excluded.last_error
                    """,
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
                    datetime.now(timezone.utc),
                    status,
                    error,
                )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
