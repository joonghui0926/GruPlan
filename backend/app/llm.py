from __future__ import annotations

import json
from typing import Any

import httpx

from .settings import Settings


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def fallback_plan_narrative(analysis: dict[str, Any]) -> str:
    parcel = analysis.get("parcel") or {}
    scores = analysis.get("scores") or {}
    tasks = analysis.get("workPlan") or []
    sources = analysis.get("sources") or []

    address = parcel.get("address") or parcel.get("pnu") or "선택한 필지"
    area = parcel.get("areaHa")
    area_text = f"{area}ha" if area not in (None, "") else "면적 확인 필요"
    scenario = scores.get("recommendedScenario") or "확인 필요"
    access = scores.get("accessibility", "확인 필요")
    disaster = scores.get("disasterRisk", "확인 필요")
    task_titles = ", ".join(str(item.get("title")) for item in tasks[:3] if item.get("title"))
    source_names = ", ".join(str(source.get("name")) for source in sources[:4] if source.get("name"))

    if not task_titles:
        task_titles = "공간분석 완료 후 작업 후보 확인"
    if not source_names:
        source_names = "연결된 공공데이터"

    return "\n".join(
        [
            f"{address}는 {area_text} 기준으로 산림 상태와 입지 조건을 함께 본 필지입니다.",
            f"현재 점수에서는 {scenario} 방향이 가장 높으며, 접근성 {access}점과 재난위험 {disaster}점을 함께 검토해야 합니다.",
            f"올해 먼저 확인할 작업은 {task_titles}입니다.",
            f"분석 출처는 {source_names}입니다. 현장 조사와 담당 기관 상담을 거쳐 최종 계획을 확정합니다.",
        ]
    )


async def generate_plan_narrative(settings: Settings, analysis: dict[str, Any]) -> str:
    fallback = fallback_plan_narrative(analysis)
    if not settings.openai_api_key:
        return fallback

    payload = _build_payload(settings.openai_model, analysis)
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    try:
        data = await _create_response(headers, payload)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            try:
                data = await _create_response(headers, _build_payload(settings.openai_model, analysis, compact=True))
            except httpx.HTTPError:
                return fallback
        else:
            return fallback
    except httpx.HTTPError:
        return fallback

    text = _extract_output_text(data).strip()
    return text[:3000] if text else fallback


def _build_payload(model: str, analysis: dict[str, Any], compact: bool = False) -> dict[str, Any]:
    instructions = (
        "너는 산림경영 상담 리포트를 작성하는 한국어 어시스턴트다. "
        "제공된 JSON 분석값 안에 있는 사실만 사용한다. "
        "점수, 주소, 출처, 작업 후보가 없으면 확인 필요라고 쓴다. "
        "전문용어는 줄이고 산주와 현장 담당자가 바로 읽을 수 있게 쓴다. "
        "4개 짧은 문단으로 작성하고 각 문단은 2문장 이하로 제한한다. "
        "마크다운 표, 글머리표, 번호 목록은 쓰지 않는다."
    )
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": json.dumps(_compact_analysis(analysis), ensure_ascii=False),
        "max_output_tokens": 700,
        "store": False,
    }
    if not compact:
        payload["reasoning"] = {"effort": "low"}
        payload["text"] = {"verbosity": "low"}
    return payload


def _compact_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "parcel": analysis.get("parcel"),
        "scores": analysis.get("scores"),
        "workPlan": analysis.get("workPlan"),
        "sources": analysis.get("sources"),
        "features": analysis.get("features"),
    }


async def _create_response(headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(OPENAI_RESPONSES_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


def _extract_output_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text

    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)
