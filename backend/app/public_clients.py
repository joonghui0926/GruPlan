from __future__ import annotations

import xml.etree.ElementTree as ET
import csv
import io
import json
from functools import lru_cache
from json import JSONDecodeError
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx

from .settings import Settings


class PublicDataError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, source_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.source_id = source_id


def _compact_xml(element: ET.Element) -> dict | str:
    children = list(element)
    if not children:
        return element.text or ""
    data: dict[str, object] = {}
    for child in children:
        value = _compact_xml(child)
        if child.tag in data:
            existing = data[child.tag]
            if not isinstance(existing, list):
                data[child.tag] = [existing]
            data[child.tag].append(value)
        else:
            data[child.tag] = value
    return data


def _upstream_message(response: httpx.Response) -> str:
    text = response.text.strip().replace("\n", " ")
    return text[:260] if text else response.reason_phrase


DEFAULT_HEADERS = {
    "Accept": "application/json, application/xml, text/xml, */*",
    "Connection": "close",
    "User-Agent": "Mozilla/5.0 (GruPlan AI; public-data client)",
}
CARBON_OFFSET_API_URL = "https://api.odcloud.kr/api/15125368/v1/uddi:33b54646-2140-44ee-ae6a-a4dbdccad253"
CARBON_OFFSET_CSV_URL = "https://www.data.go.kr/cmm/cmm/fileDownload.do"
CARBON_OFFSET_CSV_PARAMS = {
    "atchFileId": "FILE_000000003079786",
    "fileDetailSn": 1,
    "insertDataPrcus": "N",
}


def _fallback_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc.endswith("vworld.kr"):
        return urlunparse(parsed._replace(scheme="http"))
    return None


async def _get(url: str, params: dict, headers: dict[str, str] | None = None) -> httpx.Response:
    request_headers = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        async with httpx.AsyncClient(timeout=18, follow_redirects=True) as client:
            return await client.get(url, params=params, headers=request_headers)
    except httpx.RemoteProtocolError:
        fallback = _fallback_url(url)
        if not fallback:
            raise
        async with httpx.AsyncClient(timeout=18, follow_redirects=True) as client:
            return await client.get(fallback, params=params, headers=request_headers)


def _decode_public_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


async def fetch_xml(url: str, params: dict, source_id: str, headers: dict[str, str] | None = None) -> dict:
    try:
        response = await _get(url, params, headers)
        response.raise_for_status()
        body = response.text.lstrip()
        if body.startswith("{"):
            data = response.json()
            message = _provider_error_message(data)
            if message:
                raise PublicDataError(message, 502, source_id)
            return data
        if body.startswith(("<html", "<!DOCTYPE html")):
            raise PublicDataError(_friendly_upstream_message(source_id, response), 502, source_id)
        root = ET.fromstring(body)
        return {root.tag: _compact_xml(root)}
    except httpx.HTTPStatusError as exc:
        raise PublicDataError(_friendly_upstream_message(source_id, exc.response), 502, source_id) from exc
    except JSONDecodeError as exc:
        raise PublicDataError("제공기관 JSON 응답을 정리하지 못했습니다.", 502, source_id) from exc
    except ET.ParseError as exc:
        raise PublicDataError("제공기관 응답 형식이 달라 항목을 표시하지 못했습니다.", 502, source_id) from exc
    except httpx.HTTPError as exc:
        raise PublicDataError("제공기관 연결이 지연되고 있습니다.", 502, source_id) from exc


async def fetch_json(url: str, params: dict, source_id: str, headers: dict[str, str] | None = None) -> dict:
    try:
        response = await _get(url, params, headers)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        raise PublicDataError(_friendly_upstream_message(source_id, exc.response), 502, source_id) from exc
    except (httpx.HTTPError, JSONDecodeError) as exc:
        raise PublicDataError(f"제공기관 연결 확인 필요: {exc}", 502, source_id) from exc

    message = _provider_error_message(data)
    if message:
        raise PublicDataError(message, 502, source_id)
    return data


def _provider_error_message(data: dict) -> str | None:
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        return None
    if str(response.get("status", "")).upper() != "ERROR":
        return None
    error = response.get("error")
    if isinstance(error, dict):
        text = error.get("text") or error.get("message") or error.get("code")
        if text:
            return f"제공기관 응답 오류: {text}"
    return "제공기관 응답 오류"


def _friendly_upstream_message(source_id: str, response: httpx.Response) -> str:
    if response.status_code in {401, 403}:
        return {
            "D6": "산불위험예보는 제공기관 승인 상태 확인 후 표시됩니다. 현재 재난 판단은 공간분석 결과를 우선 사용합니다.",
            "D10": "산림사업법인 목록은 제공기관 승인 상태 확인 후 표시됩니다. 현재는 작업 종류와 지역 조건을 상담 준비 항목으로 남깁니다.",
            "D11": "산림자원통계는 제공기관 승인 상태 확인 후 표시됩니다. 현재 경영 판단은 필지 공간분석 결과를 우선 사용합니다.",
        }.get(source_id, "제공기관 승인 상태 확인 후 항목을 표시합니다.")
    return {
        "D6": "산불위험예보 응답이 지연되고 있습니다. 현재 재난 판단은 공간분석 결과를 우선 사용합니다.",
        "D10": "산림사업법인 응답이 지연되고 있습니다. 현재는 상담 준비 항목만 정리합니다.",
        "D11": "산림자원통계 응답이 지연되고 있습니다. 현재 경영 판단은 필지 공간분석 결과를 우선 사용합니다.",
    }.get(source_id, f"제공기관 응답을 현재 화면에 맞게 정리하지 못했습니다.")


def _current_admin_code(value: str | None, length: int | None = None) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not text:
        return ""
    province_updates = {"42": "51", "45": "52"}
    if text[:2] in province_updates:
        text = f"{province_updates[text[:2]]}{text[2:]}"
    return text[:length] if length else text


@lru_cache(maxsize=1)
def _forest_company_snapshot() -> dict | None:
    path = Path(__file__).resolve().parent / "data" / "forest_companies_snapshot.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _filter_snapshot_items(items: list[dict], *terms: str | None, limit: int = 60) -> list[dict]:
    keywords = [str(term).strip() for term in terms if term and str(term).strip()]
    if not keywords:
        return items[:limit]

    matched = []
    for item in items:
        text = " ".join(str(value) for value in item.values() if value is not None)
        if all(keyword in text for keyword in keywords):
            matched.append(item)
        if len(matched) >= limit:
            break
    return matched


def _snapshot_response(snapshot: dict, items: list[dict]) -> dict:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "OFFICIAL SNAPSHOT"},
            "body": {
                "items": {"item": items},
                "numOfRows": len(items),
                "pageNo": 1,
                "totalCount": len(items),
            },
        },
        "snapshot": {
            "sourceId": snapshot.get("sourceId"),
            "sourceName": snapshot.get("sourceName"),
            "sourceUrl": snapshot.get("sourceUrl"),
            "fetchedAt": snapshot.get("fetchedAt"),
            "totalCount": snapshot.get("totalCount"),
        },
    }


class PublicApiClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def vworld_headers(self) -> dict[str, str]:
        referer = self.settings.vworld_referer.rstrip("/") + "/"
        return {
            "Origin": referer.rstrip("/"),
            "Referer": referer,
        }

    def vworld_domain(self) -> str:
        return self.settings.vworld_referer.rstrip("/")

    def require_data_key(self, source_id: str) -> str:
        if not self.settings.data_go_kr_service_key:
            raise PublicDataError("DATA_GO_KR_SERVICE_KEY가 설정되지 않았습니다.", 503, source_id)
        return self.settings.data_go_kr_service_key

    def require_vworld_key(self) -> str:
        if not self.settings.vworld_api_key:
            raise PublicDataError("VWORLD_API_KEY가 설정되지 않았습니다.", 503, "D12")
        return self.settings.vworld_api_key

    async def search_address(self, query: str) -> dict:
        key = self.require_vworld_key()
        params = {
            "service": "search",
            "request": "search",
            "version": "2.0",
            "crs": "EPSG:4326",
            "size": 10,
            "page": 1,
            "type": "address",
            "category": "parcel",
            "format": "json",
            "errorFormat": "json",
            "domain": self.vworld_domain(),
            "query": query,
            "key": key,
        }
        return await fetch_json("https://api.vworld.kr/req/search", params, "D12")

    async def cadastral_by_point(self, lon: float, lat: float) -> dict:
        key = self.require_vworld_key()
        params = {
            "service": "data",
            "request": "GetFeature",
            "data": "lp_pa_cbnd_bubun",
            "geomFilter": f"POINT({lon} {lat})",
            "crs": "EPSG:4326",
            "format": "json",
            "errorFormat": "json",
            "geometry": "true",
            "attribute": "true",
            "domain": self.vworld_domain(),
            "size": 10,
            "key": key,
        }
        return await fetch_json("https://api.vworld.kr/req/data", params, "D12")

    async def cadastral_by_pnu(self, pnu: str) -> dict:
        key = self.require_vworld_key()
        params = {
            "service": "data",
            "request": "GetFeature",
            "data": "lp_pa_cbnd_bubun",
            "attrFilter": f"pnu:like:{pnu}",
            "crs": "EPSG:4326",
            "format": "json",
            "errorFormat": "json",
            "geometry": "true",
            "attribute": "true",
            "domain": self.vworld_domain(),
            "size": 10,
            "key": key,
        }
        return await fetch_json("https://api.vworld.kr/req/data", params, "D12")

    async def mountain_weather(self, obsid: str | None = None, local_area: str | None = None) -> dict:
        key = self.require_data_key("D7")
        params = {
            "ServiceKey": key,
            "pageNo": 1,
            "numOfRows": 10,
            "_type": "json",
        }
        if obsid:
            params["obsid"] = obsid
        if local_area and str(local_area).isdigit():
            params["localArea"] = local_area
        return await fetch_json("http://apis.data.go.kr/1400377/mtweather/mountListSearch", params, "D7")

    async def economic_forest(self, search: str | None = None, frst_type: str | None = None) -> dict:
        params = {"pageNo": 1, "numOfRows": 20}
        if search:
            params["searchPlcNm"] = search
        if frst_type:
            params["frstType"] = frst_type
        return await self._fetch_forest_xml(
            "http://api.forest.go.kr/openapi/service/fsInfoService/ecoFrstyOpenAPI",
            params,
            "D8",
        )

    async def carbon_offset_projects(self, page: int = 1, per_page: int = 20) -> dict:
        page = max(1, page)
        per_page = min(max(1, per_page), 1000)
        key = self.settings.data_go_kr_service_key
        if key:
            params = {
                "page": page,
                "perPage": per_page,
                "serviceKey": key,
                "returnType": "JSON",
            }
            try:
                return await fetch_json(CARBON_OFFSET_API_URL, params, "D9")
            except PublicDataError:
                pass
        try:
            response = await _get(
                CARBON_OFFSET_CSV_URL,
                CARBON_OFFSET_CSV_PARAMS,
                headers={
                    "Accept": "text/csv, application/octet-stream, */*",
                    "Referer": "https://www.data.go.kr/data/15125368/fileData.do?recommendDataYn=Y",
                },
            )
            response.raise_for_status()
            text = _decode_public_csv(response.content)
            rows = list(csv.DictReader(io.StringIO(text)))
            start = (page - 1) * per_page
            items = rows[start : start + per_page]
            return {
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "OFFICIAL CSV"},
                    "body": {
                        "items": {"item": items},
                        "numOfRows": len(items),
                        "pageNo": page,
                        "totalCount": len(rows),
                    },
                },
                "data": items,
                "page": page,
                "perPage": per_page,
                "currentCount": len(items),
                "totalCount": len(rows),
                "source": {
                    "sourceId": "D9",
                    "sourceName": "산림청_산림탄소상쇄사업 등록 현황",
                    "sourceUrl": "https://www.data.go.kr/data/15125368/fileData.do",
                },
            }
        except httpx.HTTPStatusError as exc:
            raise PublicDataError(_friendly_upstream_message("D9", exc.response), 502, "D9") from exc
        except (httpx.HTTPError, csv.Error) as exc:
            raise PublicDataError(f"제공기관 연결 확인 필요: {exc}", 502, "D9") from exc

    async def forest_companies(
        self,
        trade_name: str | None = None,
        captain: str | None = None,
        region: str | None = None,
    ) -> dict:
        snapshot = _forest_company_snapshot()
        if region and snapshot:
            items = snapshot.get("items") or []
            filtered = _filter_snapshot_items(items, region, trade_name, captain)
            if filtered:
                return _snapshot_response(snapshot, filtered)

        params = {"pageNo": 1, "numOfRows": 20}
        if trade_name:
            params["tradeName"] = trade_name
        if captain:
            params["captain"] = captain
        try:
            return await self._fetch_forest_xml(
                "http://api.forest.go.kr/openapi/service/fsInfoService/corInfoOpenAPI",
                params,
                "D10",
            )
        except PublicDataError:
            if snapshot:
                items = snapshot.get("items") or []
                filtered = _filter_snapshot_items(items, region, trade_name, captain)
                return _snapshot_response(snapshot, filtered)
            return {
                "items": [],
                "notice": "산림사업법인 목록은 제공기관 응답 형식 확인 후 다시 표시됩니다. 현재 리포트에서는 작업 종류와 지역 조건만 상담 준비 항목으로 남깁니다.",
                "sourceId": "D10",
            }

    async def resource_stats(self, class_id: str | None = None) -> dict:
        key = self.require_data_key("D11")
        params = {"serviceKey": key, "pageNo": 1, "numOfRows": 20}
        if class_id:
            params["clsscId"] = class_id
        try:
            return await fetch_json("http://apis.data.go.kr/1400000/frsas1/selectStatList1", params, "D11")
        except PublicDataError:
            return {
                "items": [],
                "notice": "산림자원통계는 제공기관 승인 상태 확인 후 표시됩니다. 현재 경영 판단은 필지 공간분석 결과를 우선 사용합니다.",
                "sourceId": "D11",
            }

    async def fire_risk(self, **params) -> dict:
        key = self.require_data_key("D6")
        sigungu_code = _current_admin_code(params.get("sigunguCode"), 5)
        sido_code = _current_admin_code(params.get("sidoCode"), 2)
        query = {
            "ServiceKey": key,
            "pageNo": 1,
            "numOfRows": 20,
            "_type": "json",
            "excludeForecast": 0,
        }
        if sigungu_code:
            url = "https://apis.data.go.kr/1400377/forestPointV2/forestPointListSigunguSearchV2"
            query["localAreas"] = sigungu_code
            query["upplocalcd"] = str(sigungu_code)[:2]
        elif sido_code:
            url = "https://apis.data.go.kr/1400377/forestPointV2/forestPointListSidoSearchV2"
            query["localAreas"] = sido_code
        else:
            url = "https://apis.data.go.kr/1400377/forestPointV2/forestPointListGeongugSearchV2"
        return await fetch_json(url, query, "D6")

    async def _fetch_forest_xml(self, url: str, base_params: dict, source_id: str) -> dict:
        key = self.require_data_key(source_id)
        errors: list[PublicDataError] = []
        for key_name in ("ServiceKey", "serviceKey"):
            params = {**base_params, key_name: key}
            try:
                return await fetch_xml(url, params, source_id, {"Accept": "application/xml, text/xml, */*"})
            except PublicDataError as exc:
                errors.append(exc)
        raise errors[-1]
