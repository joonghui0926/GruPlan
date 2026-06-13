from __future__ import annotations

import xml.etree.ElementTree as ET
from json import JSONDecodeError
from urllib.parse import urlencode

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


async def fetch_xml(url: str, params: dict, source_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=18) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
        root = ET.fromstring(response.text)
        return {root.tag: _compact_xml(root)}
    except httpx.HTTPStatusError as exc:
        raise PublicDataError(f"제공기관 응답 오류: {_upstream_message(exc.response)}", 502, source_id) from exc
    except (httpx.HTTPError, ET.ParseError) as exc:
        raise PublicDataError(f"제공기관 연결 확인 필요: {exc}", 502, source_id) from exc


async def fetch_json(url: str, params: dict, source_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=18) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        raise PublicDataError(f"제공기관 응답 오류: {_upstream_message(exc.response)}", 502, source_id) from exc
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


class PublicApiClient:
    def __init__(self, settings: Settings):
        self.settings = settings

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
            "query": query,
            "key": key,
        }
        return await fetch_json("https://api.vworld.kr/req/search", params, "D12")

    async def cadastral_by_point(self, lon: float, lat: float) -> dict:
        key = self.require_vworld_key()
        params = {
            "service": "data",
            "request": "GetFeature",
            "data": "LP_PA_CBND_BUBUN",
            "geomFilter": f"POINT({lon} {lat})",
            "crs": "EPSG:4326",
            "format": "json",
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
        if local_area:
            params["localArea"] = local_area
        return await fetch_json("http://apis.data.go.kr/1400377/mtweather/mountListSearch", params, "D7")

    async def economic_forest(self, search: str | None = None, frst_type: str | None = None) -> dict:
        key = self.require_data_key("D8")
        params = {"ServiceKey": key, "pageNo": 1, "numOfRows": 20}
        if search:
            params["searchPlcNm"] = search
        if frst_type:
            params["frstType"] = frst_type
        return await fetch_xml("http://api.forest.go.kr/openapi/service/fsInfoService/ecoFrstyOpenAPI", params, "D8")

    async def forest_companies(self, trade_name: str | None = None, captain: str | None = None) -> dict:
        key = self.require_data_key("D10")
        params = {"ServiceKey": key, "pageNo": 1, "numOfRows": 20}
        if trade_name:
            params["tradeName"] = trade_name
        if captain:
            params["captain"] = captain
        return await fetch_xml("http://api.forest.go.kr/openapi/service/fsInfoService/corInfoOpenAPI", params, "D10")

    async def resource_stats(self, class_id: str | None = None) -> dict:
        key = self.require_data_key("D11")
        params = {"serviceKey": key, "pageNo": 1, "numOfRows": 20}
        if class_id:
            params["clsscId"] = class_id
        return await fetch_json("http://apis.data.go.kr/1400000/frsas1/selectStatList1", params, "D11")

    async def fire_risk(self, **params) -> dict:
        key = self.require_data_key("D6")
        if not self.settings.fire_risk_endpoint:
            raise PublicDataError(
                "산불위험예보 상세 호출 주소는 공공데이터포털 Swagger에서 확인 후 FIRE_RISK_ENDPOINT로 설정해야 합니다.",
                503,
                "D6",
            )
        query = {"ServiceKey": key, **{k: v for k, v in params.items() if v is not None}}
        separator = "&" if "?" in self.settings.fire_risk_endpoint else "?"
        url = self.settings.fire_risk_endpoint + separator + urlencode(query)
        async with httpx.AsyncClient(timeout=18) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                ctype = response.headers.get("content-type", "")
                if "json" in ctype:
                    return response.json()
                root = ET.fromstring(response.text)
                return {root.tag: _compact_xml(root)}
            except httpx.HTTPStatusError as exc:
                raise PublicDataError(f"제공기관 응답 오류: {_upstream_message(exc.response)}", 502, "D6") from exc
            except (httpx.HTTPError, ET.ParseError, JSONDecodeError) as exc:
                raise PublicDataError(f"제공기관 연결 확인 필요: {exc}", 502, "D6") from exc
