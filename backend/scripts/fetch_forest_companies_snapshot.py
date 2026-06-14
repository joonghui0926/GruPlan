from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx


SOURCE_URL = "http://api.forest.go.kr/openapi/service/fsInfoService/corInfoOpenAPI"
PAGE_SIZE = 1000


async def fetch_page(client: httpx.AsyncClient, page: int) -> tuple[int, list[dict]]:
    response = await client.get(
        SOURCE_URL,
        params={
            "serviceKey": os.environ["DATA_GO_KR_SERVICE_KEY"],
            "pageNo": page,
            "numOfRows": PAGE_SIZE,
        },
        headers={"Accept": "application/json, */*"},
    )
    response.raise_for_status()
    data = response.json()
    body = data["response"]["body"]
    items = body.get("items", {}).get("item", [])
    if isinstance(items, dict):
        items = [items]
    return int(body.get("totalCount") or 0), items


async def main() -> None:
    async with httpx.AsyncClient(timeout=80, follow_redirects=True) as client:
        total, first_items = await fetch_page(client, 1)
        items = list(first_items)
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        for page in range(2, pages + 1):
            _, page_items = await fetch_page(client, page)
            items.extend(page_items)

    payload = {
        "sourceId": "D10",
        "sourceName": "산림청_산림사업법인 정보",
        "sourceUrl": SOURCE_URL,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "totalCount": total,
        "items": items,
    }
    target = Path(__file__).resolve().parents[1] / "app" / "data" / "forest_companies_snapshot.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {len(items)} of {total} items to {target}")


if __name__ == "__main__":
    asyncio.run(main())
