from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .data_catalog import PUBLIC_DATA_SOURCES, SOURCE_BY_ID
from .db import Database
from .llm import generate_plan_narrative
from .public_clients import PublicApiClient, PublicDataError
from .reports import build_plan_pdf
from .scoring import FeatureSet, distance_score, score_features, slope_penalty
from .settings import get_settings


settings = get_settings()
db = Database(settings)
public_client = PublicApiClient(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    yield
    await db.close()


app = FastAPI(title="Gruplan AI GIS Backend", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ParcelLookupRequest(BaseModel):
    query: str | None = Field(default=None, description="지번 또는 주소")
    lat: float | None = None
    lon: float | None = None


class AnalysisRequest(BaseModel):
    pnu: str | None = None
    geometry: dict | None = None
    include_live: bool = True


class ReportRequest(BaseModel):
    analysis: dict


def _public_error(exc: PublicDataError) -> dict:
    return {"error": {"message": str(exc), "sourceId": exc.source_id}}


@app.get("/")
async def index():
    html = Path(__file__).resolve().parents[2] / "gruplan.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="gruplan.html을 찾을 수 없습니다.")
    return FileResponse(html)


@app.get("/api/health")
async def health():
    return {
        "app": settings.app_name,
        "database": await db.status(),
        "keys": {
            "DATA_GO_KR_SERVICE_KEY": bool(settings.data_go_kr_service_key),
            "VWORLD_API_KEY": bool(settings.vworld_api_key),
            "OPENAI_API_KEY": bool(settings.openai_api_key),
            "FIRE_RISK_ENDPOINT": bool(settings.fire_risk_endpoint),
        },
        "llm": {"model": settings.openai_model},
        "sources": len(PUBLIC_DATA_SOURCES),
    }


@app.get("/api/data-sources")
async def data_sources():
    loaded_tables = await _loaded_tables()
    configured_keys = {
        "data": bool(settings.data_go_kr_service_key),
        "vworld": bool(settings.vworld_api_key),
    }
    items = []
    for source in PUBLIC_DATA_SOURCES:
        status = "연결 가능"
        if source.table_name:
            status = "공간 DB 연결" if source.table_name in loaded_tables else "스키마 확인 필요"
        if source.requires_key:
            key_ready = configured_keys["vworld"] if source.id == "D12" else configured_keys["data"]
            status = "API 키 확인 필요" if not key_ready else status
        item = source.to_dict()
        item["status"] = status
        items.append(item)
    return {"items": items}


@app.get("/api/client-config")
async def client_config():
    return {
        "vworldKey": settings.vworld_api_key,
        "vworldDomain": settings.vworld_referer,
    }


@app.post("/api/parcels/lookup")
async def lookup_parcel(payload: ParcelLookupRequest):
    try:
        if payload.lat is not None and payload.lon is not None:
            return await public_client.cadastral_by_point(payload.lon, payload.lat)
        if payload.query:
            search = await public_client.search_address(payload.query)
            point = _first_vworld_point(search)
            if not point:
                return {"search": search, "cadastral": None}
            cadastral = await public_client.cadastral_by_point(point["lon"], point["lat"])
            return {"search": search, "cadastral": cadastral}
    except PublicDataError as exc:
        return _public_error(exc)
    raise HTTPException(status_code=400, detail="query 또는 lat/lon을 입력하세요.")


@app.post("/api/analysis")
async def analyze_parcel(payload: AnalysisRequest):
    missing = await _missing_required_spatial_tables()
    if missing:
        raise HTTPException(
            status_code=424,
            detail={
                "message": "공간분석 원본 데이터 적재가 필요합니다.",
                "missingSources": missing,
                "nextStep": "backend/scripts/ingest_spatial.py로 원본 SHP와 raster를 PostGIS에 적재하세요.",
            },
        )
    if not payload.pnu and not payload.geometry:
        raise HTTPException(status_code=400, detail="pnu 또는 geometry가 필요합니다.")

    row = await _query_spatial_features(payload)
    if row is None:
        raise HTTPException(status_code=404, detail="해당 필지를 찾지 못했습니다.")

    raw_features = row["features"] or {}
    features = FeatureSet(
        area_ha=float(row["area_ha"]) if row["area_ha"] is not None else None,
        road_distance_m=float(row["road_distance_m"]) if row["road_distance_m"] is not None else None,
        road_density_m_per_ha=float(row["road_density_m_per_ha"]) if row["road_density_m_per_ha"] is not None else None,
        slope_degree=_number_or_none(row["slope_degree"]),
        avg_landslide_grade=_number_or_none(row["avg_landslide_grade"]),
        high_landslide_ratio=_number_or_none(row["high_landslide_ratio"]),
        economic_forest=bool(row["economic_forest"]),
        planting_fit_count=int(row["planting_fit_count"] or 0),
        stand_age_class=int(row["stand_age_class"]) if row["stand_age_class"] is not None else None,
    )
    scores = score_features(features)
    return {
        "parcel": {
            "pnu": row["pnu"],
            "address": row["address"],
            "areaHa": features.area_ha,
            "adminName": row["admin_name"],
        },
        "features": raw_features,
        "scores": scores,
        "xai": _build_xai(features, scores),
        "workPlan": _work_plan(scores),
        "scenarioReasons": _scenario_reasons(features, scores),
        "scenarioPlan": _scenario_plan(features, scores, raw_features),
        "sources": _analysis_sources(features, scores),
    }


@app.get("/api/live/mountain-weather")
async def mountain_weather(obsid: str | None = None, localArea: str | None = None):
    try:
        return await public_client.mountain_weather(obsid=obsid, local_area=localArea)
    except PublicDataError as exc:
        return _public_error(exc)


@app.get("/api/live/fire-risk")
async def fire_risk(sigunguCode: str | None = None):
    try:
        return await public_client.fire_risk(sigunguCode=sigunguCode)
    except PublicDataError as exc:
        return _public_error(exc)


@app.get("/api/forest-companies")
async def forest_companies(tradeName: str | None = None, captain: str | None = None, region: str | None = None):
    try:
        return await public_client.forest_companies(trade_name=tradeName, captain=captain, region=region)
    except PublicDataError as exc:
        return _public_error(exc)


@app.get("/api/economic-forest")
async def economic_forest(search: str | None = None, frstType: str | None = None):
    try:
        return await public_client.economic_forest(search=search, frst_type=frstType)
    except PublicDataError as exc:
        return _public_error(exc)


@app.get("/api/resource-stats")
async def resource_stats(classId: str | None = None):
    try:
        return await public_client.resource_stats(class_id=classId)
    except PublicDataError as exc:
        return _public_error(exc)


@app.post("/api/reports/plan")
async def plan_report(payload: ReportRequest, _: BackgroundTasks):
    analysis = dict(payload.analysis)
    analysis["narrative"] = await generate_plan_narrative(settings, analysis)
    pdf = build_plan_pdf(analysis)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="gruplan-forest-plan.pdf"'},
    )


async def _loaded_tables() -> set[str]:
    if not db.pool:
        return set()
    rows = await db.fetch(
        """
        select table_name
        from information_schema.tables
        where table_schema = 'public'
        """
    )
    return {row["table_name"] for row in rows}


async def _missing_required_spatial_tables() -> list[dict]:
    loaded = await _loaded_tables()
    required = ["D1", "D2", "D3", "D4", "D5", "D8", "D12"]
    missing = []
    for source_id in required:
        source = SOURCE_BY_ID[source_id]
        if source.table_name not in loaded:
            missing.append({"id": source.id, "name": source.name, "table": source.table_name})
    return missing


async def _query_spatial_features(payload: AnalysisRequest):
    sql = """
    with parcel as (
      select pnu, address, admin_name, geom
      from parcels
      where ($1::text is not null and pnu = $1)
      union all
      select $1::text as pnu, null::text as address, null::text as admin_name,
             ST_SetSRID(ST_GeomFromGeoJSON($2), 4326) as geom
      where $2::text is not null
        and not exists (
          select 1 from parcels
          where $1::text is not null and pnu = $1
        )
      limit 1
    ),
    area_calc as (
      select pnu, address, admin_name, geom, ST_Area(geom::geography) / 10000.0 as area_ha
      from parcel
    ),
    nearest_road as (
      select min(ST_Distance(a.geom::geography, r.geom::geography)) as road_distance_m
      from area_calc a, forest_roads r
      where ST_DWithin(a.geom::geography, r.geom::geography, 5000)
    ),
    road_density as (
      select coalesce(sum(ST_Length(ST_Intersection(a.geom, r.geom)::geography)), 0) / nullif(max(a.area_ha), 0) as road_density_m_per_ha
      from area_calc a
      left join forest_roads r on ST_Intersects(a.geom, r.geom)
    ),
    soil as (
      select properties
      from area_calc a
      join forest_soils s on ST_Intersects(a.geom, s.geom)
      order by ST_Area(ST_Intersection(a.geom, s.geom)) desc
      limit 1
    ),
    stand as (
      select properties
      from area_calc a
      join forest_stands s on ST_Intersects(a.geom, s.geom)
      order by ST_Area(ST_Intersection(a.geom, s.geom)) desc
      limit 1
    ),
    planting as (
      select count(*) as planting_fit_count
      from area_calc a
      join planting_zones z on ST_Intersects(a.geom, z.geom)
    ),
    economic as (
      select exists (
        select 1 from area_calc a join economic_forest_zones e on ST_Intersects(a.geom, e.geom)
      ) as economic_forest
    ),
    surrounding_parcels as (
      select count(*) as nearby_parcel_count
      from area_calc a
      join parcels p on p.pnu is distinct from a.pnu
       and ST_DWithin(a.geom::geography, p.geom::geography, 250)
    ),
    road_context as (
      select count(*) as nearby_road_count
      from area_calc a
      join forest_roads r on ST_DWithin(a.geom::geography, r.geom::geography, 500)
    ),
    economic_context as (
      select exists (
        select 1
        from area_calc a
        join economic_forest_zones e on ST_DWithin(a.geom::geography, e.geom::geography, 300)
      ) as economic_zone_nearby
    ),
    landslide as (
      select
        (ST_SummaryStatsAgg(
          ST_Clip(l.rast, ST_Transform(a.geom, ST_SRID(l.rast))),
          1,
          true
        )).mean as avg_landslide_grade
      from area_calc a
      join landslide_risk l on ST_Intersects(l.rast, ST_Transform(a.geom, ST_SRID(l.rast)))
    )
    select
      a.pnu,
      a.address,
      a.admin_name,
      a.area_ha,
      nr.road_distance_m,
      rd.road_density_m_per_ha,
      (soil.properties->>'slope_degree') as slope_degree,
      landslide.avg_landslide_grade,
      null::numeric as high_landslide_ratio,
      economic.economic_forest,
      planting.planting_fit_count,
      nullif((stand.properties->>'age_class'), '')::int as stand_age_class,
      jsonb_build_object(
        'soil', soil.properties,
        'stand', stand.properties,
        'surrounding', jsonb_build_object(
          'nearbyParcelCount250m', surrounding_parcels.nearby_parcel_count,
          'nearbyRoadCount500m', road_context.nearby_road_count,
          'economicZoneNearby300m', economic_context.economic_zone_nearby
        )
      ) as features
    from area_calc a
    cross join nearest_road nr
    cross join road_density rd
    cross join planting
    cross join economic
    cross join surrounding_parcels
    cross join road_context
    cross join economic_context
    left join landslide on true
    left join soil on true
    left join stand on true
    """
    geometry = None
    if payload.geometry:
        import json

        geometry = json.dumps(payload.geometry)
    return await db.fetchrow(sql, payload.pnu, geometry)


def _number_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_vworld_point(search: dict) -> dict | None:
    items = search.get("response", {}).get("result", {}).get("items", [])
    if not items:
        return None
    point = items[0].get("point") or {}
    try:
        return {"lon": float(point["x"]), "lat": float(point["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def _work_plan(scores: dict) -> list[dict]:
    scenario = scores.get("recommendedScenario")
    if scenario == "기초조사 우선":
        return [
            {"title": "현장 기본값 채우기", "reason": "임도 거리, 경사, 수종, 영급, 배수 흔적을 먼저 확인", "season": "첫 방문"},
            {"title": "확인값 반영 후 재분석", "reason": "수익형, 탄소형, 보전형 중 실행 방향을 다시 비교", "season": "상담 전"},
        ]
    if scenario == "수익형 경영":
        return [
            {"title": "임도 접근성 기반 작업 구역 확정", "reason": "운반비와 장비 진입성 우선 확인", "season": "봄"},
            {"title": "조림 적합 수종 현장 검토", "reason": "맞춤형조림지도 결과를 현장 조건과 대조", "season": "봄"},
        ]
    if scenario == "탄소형 경영":
        return [
            {"title": "탄소상쇄 가능 면적 산정", "reason": "면적과 수종, 영급 기준 검토", "season": "상반기"},
            {"title": "등록 사례 비교표 작성", "reason": "산림탄소상쇄사업 등록 현황과 유사도 확인", "season": "상반기"},
        ]
    if scenario == "보전형 경영":
        return [
            {"title": "고위험 사면 현장 점검", "reason": "산사태위험지도와 토양 경사 지표 반영", "season": "우기 전"},
            {"title": "보전 관리구역 설정", "reason": "작업보다 안정성 확보가 우선", "season": "연중"},
        ]
    return [
        {"title": "산불과 산사태 위험 구역 표시", "reason": "재난위험 점수가 높은 필지 우선 관리", "season": "즉시"},
        {"title": "사방사업과 방제 상담 요청", "reason": "지역 산림사업법인 매칭 필요", "season": "우기 전"},
    ]


def _analysis_sources(features: FeatureSet, scores: dict) -> list[dict]:
    sources = []
    for source in PUBLIC_DATA_SOURCES:
        if source.id not in {"D1", "D2", "D3", "D4", "D5", "D8", "D12"}:
            continue
        item = source.to_dict()
        item["status"] = "분석 근거"
        item["analysisDetail"] = _source_analysis_detail(source.id)
        item["parcelEvidence"] = _source_parcel_evidence(source.id, features, scores)
        sources.append(item)
    return sources


def _scenario_reasons(features: FeatureSet, scores: dict) -> list[dict]:
    access = scores.get("accessibility", 0) or 0
    disaster = scores.get("disasterRisk", 0) or 0
    area = features.area_ha or 0
    age = features.stand_age_class or 0
    planting = features.planting_fit_count or 0
    economic = "경제림 구역에 포함" if features.economic_forest else "경제림 구역 미포함"
    road = _format_meters(features.road_distance_m)
    slope = _format_degree(features.slope_degree)
    landslide = _format_grade(features.avg_landslide_grade)
    density = _format_density(features.road_density_m_per_ha)
    gaps = _gap_text(scores)
    access_ready = features.road_distance_m is not None and features.slope_degree is not None
    disaster_ready = features.avg_landslide_grade is not None or features.high_landslide_ratio is not None
    stand_ready = features.stand_age_class is not None
    profit_judgement = (
        f"이 필지는 {economic}이고 조림 후보가 {planting}건입니다. 임도 거리 {road}, 임도 밀도 {density}, 경사 {slope}가 함께 들어가 접근성 {round(access)}점으로 계산됐습니다."
        if access_ready
        else f"수익형은 작업로와 운반 동선이 핵심인데, 이 필지는 {gaps} 확보가 먼저입니다. 현재 {round(scores.get('profit', 0) or 0)}점은 경제림 여부와 조림 후보만 반영한 예비값이며, 임도 거리와 경사가 들어오면 가장 크게 바뀝니다."
    )
    carbon_judgement = (
        f"면적 {round(area, 2)}ha와 영급 {age}이 장기 흡수 관리 점수를 만들었습니다. 면적 기여는 {round(min(area * 1.6, 14), 1)}점, 영급 기여는 {round(min(age * 3.5, 22), 1)}점입니다."
        if stand_ready
        else f"면적 {round(area, 2)}ha는 확인됐지만 영급과 수종 근거가 아직 비어 있습니다. 탄소형 {round(scores.get('carbon', 0) or 0)}점은 면적만 반영한 출발값이고, 실제 등록성은 영급·수종·제외면적 산정 뒤에 좁혀야 합니다."
    )
    conservation_judgement = (
        f"산사태 평균등급은 {landslide}, 경사는 {slope}입니다. 이 조합이 보전형 {round(scores.get('conservation', 0) or 0)}점을 만들었고, 급경사나 위험 격자가 겹치는 구역은 별도 관리 대상으로 봅니다."
        if disaster_ready and features.slope_degree is not None
        else f"보전형은 경사와 산사태 위험이 핵심인데, 현재는 {gaps} 확보가 필요합니다. 그래서 보전형 {round(scores.get('conservation', 0) or 0)}점은 안전 판정이 아니라 계곡부·급경사·배수 흔적을 먼저 확인하라는 신호입니다."
    )
    resilience_judgement = (
        f"재난위험 {round(disaster)}점과 접근성 {round(access)}점이 함께 들어갔습니다. 위험이 있고 접근이 어려운 구간은 장마 전 배수와 임도 유실 점검을 앞순위로 둡니다."
        if disaster_ready and access_ready
        else f"재난저감형은 산사태 위험, 산불위험, 접근 동선을 같이 봐야 합니다. 현재 {round(scores.get('resilience', 0) or 0)}점은 면적과 확인된 일부 항목만 반영한 예비값이며, 물길·임도·사면 하단을 먼저 확인해야 합니다."
    )

    rows = [
        {
            "scenario": "수익형",
            "score": scores.get("profit", 0),
            "judgement": profit_judgement,
            "drivers": [
                f"경제림 여부: {economic}",
                f"조림 후보: {planting}건",
                f"작업 동선: {road} / 경사 {slope}",
            ],
            "nextCheck": "작업로 실제 통행, 벌채와 운반 동선, 사업비 대비 수익성을 현장 견적으로 확인합니다.",
            "sourceIds": ["D1", "D3", "D4", "D8"],
        },
        {
            "scenario": "탄소형",
            "score": scores.get("carbon", 0),
            "judgement": carbon_judgement,
            "drivers": [
                f"면적: {round(area, 2)}ha",
                f"영급: {age if age else '확인 필요'}",
                "제외면적: 현장 산정 필요",
            ],
            "nextCheck": "수종, 영급, 제외 면적을 보정한 뒤 산림탄소상쇄 등록 사례와 유사 면적을 비교합니다.",
            "sourceIds": ["D1", "D9", "D12"],
        },
        {
            "scenario": "보전형",
            "score": scores.get("conservation", 0),
            "judgement": conservation_judgement,
            "drivers": [
                f"경사: {slope}",
                f"산사태 평균등급: {landslide}",
                f"배수·계곡부: 현장 확인",
            ],
            "nextCheck": "계곡부, 급경사 경계, 배수 불량 지점이 일부라도 있으면 해당 구역만 보전 구역으로 분리합니다.",
            "sourceIds": ["D2", "D5"],
        },
        {
            "scenario": "재난저감",
            "score": scores.get("resilience", 0),
            "judgement": resilience_judgement,
            "drivers": [
                f"재난위험: {round(disaster)}점",
                f"진입 동선: {road}",
                f"면적: {round(area, 2)}ha",
            ],
            "nextCheck": "장마 전 배수, 임도 유실, 산불 진입 동선을 먼저 확인하고 위험 구간별 작업 순서를 나눕니다.",
            "sourceIds": ["D4", "D5", "D6", "D12"],
        },
    ]
    return sorted(rows, key=lambda item: item["score"] or 0, reverse=True)


def _scenario_plan(features: FeatureSet, scores: dict, raw_features: dict) -> dict:
    context = raw_features.get("surrounding") if isinstance(raw_features, dict) else {}
    context = context or {}
    scenario_scores = _scenario_score_map(scores)
    ranked = sorted(scenario_scores.items(), key=lambda item: item[1], reverse=True)
    top_name, top_score = ranked[0]
    second_name, second_score = ranked[1] if len(ranked) > 1 else ("비교 대상", 0)
    margin = round((top_score or 0) - (second_score or 0), 1)
    nearby_parcels = int(context.get("nearbyParcelCount250m") or 0)
    nearby_roads = int(context.get("nearbyRoadCount500m") or 0)
    nearby_economic = bool(context.get("economicZoneNearby300m"))
    area = features.area_ha or 0
    age = features.stand_age_class or 0
    access = scores.get("accessibility", 0) or 0
    disaster = scores.get("disasterRisk", 0) or 0
    road = _format_meters(features.road_distance_m)
    slope = _format_degree(features.slope_degree)

    if scores.get("recommendedScenario") == "기초조사 우선":
        top_name = "기초조사 우선"
        top_score = ranked[0][1]
        thesis = (
            f"이 필지는 면적 {round(area, 2)}ha와 경계는 잡혔지만 {_gap_text(scores)} 확보가 먼저입니다. "
            "지금 바로 수익형이나 탄소형으로 확정하기보다, 하루 현장 확인으로 작업로·수종·위험 구간을 채운 뒤 다시 점수를 계산하는 편이 맞습니다."
        )
        context = [
            f"주변 250m 안의 필지는 {nearby_parcels}개, 500m 안의 임도 요소는 {nearby_roads}개입니다.",
            f"현재 잠정 1순위 점수는 {ranked[0][0]} {round(ranked[0][1])}점이지만, 누락된 핵심 근거가 들어오면 순위가 바뀔 수 있습니다.",
            "작은 필지일수록 전체 사업보다 진입 동선, 제외 면적, 관리 가능한 구역을 먼저 나누는 것이 비용을 줄입니다.",
        ]
        ideas = [
            "필지를 세 구역으로 나눕니다: 접근 가능한 관리 구역, 손대지 않는 보전 구역, 경계와 물길 확인 구역.",
            "첫 방문에서는 벌채 가능성보다 작업로, 경사 체감, 물길, 수종, 고사목을 한 장의 현장 체크표로 채웁니다.",
            "조사 뒤 수익형, 탄소형, 보전형 중 하나로 확정하지 말고 두 개의 혼합안까지 비교합니다.",
        ]
        phases = [
            {"name": "1단계 지도 보정", "actions": ["필지 경계와 실제 진입 위치 확인", "임도와 차량 접근 가능 지점 표시", "계곡부와 배수 흐름 표시"]},
            {"name": "2단계 현장 체크", "actions": ["대표 수종과 영급 확인", "경사와 장비 투입 가능성 기록", "산사태 흔적과 임도 유실 여부 확인"]},
            {"name": "3단계 재분석", "actions": ["확인값을 넣어 시나리오 재계산", "작업 구역과 제외 구역 분리", "법인 상담용 요청 범위 작성"]},
        ]
        risks = [
            "임도 거리나 경사가 비어 있는 상태에서 사업비를 말하면 실제 견적과 크게 달라질 수 있습니다.",
            "산사태 위험이 확인되지 않았다고 안전한 필지로 보면 안 됩니다.",
            "영급과 수종이 비어 있으면 탄소형 판단은 등록 가능성보다 조사 후보 수준으로 봐야 합니다.",
        ]
        return {
            "scenario": top_name,
            "score": top_score,
            "secondScenario": second_name,
            "margin": margin,
            "thesis": thesis,
            "context": context,
            "ideas": ideas,
            "phases": phases,
            "risks": risks,
        }

    common_context = [
        f"추천 점수는 {top_name} {round(top_score)}점이며, 다음 후보인 {second_name}보다 {margin}점 높습니다.",
        f"주변 250m 안의 필지는 {nearby_parcels}개, 500m 안의 임도 요소는 {nearby_roads}개입니다.",
    ]
    if nearby_economic:
        common_context.append("주변 300m 안에 경제림 구역 근거가 있어 생산형 사업과의 충돌 여부를 함께 봅니다.")

    if top_name == "탄소형":
        thesis = f"이 필지는 면적 {round(area, 2)}ha와 영급 {age if age else '미확인'}이 탄소형 점수를 끌어올렸습니다. 접근성 {round(access)}점과 재난위험 {round(disaster)}점은 등록보다 유지관리 비용과 모니터링 설계에서 더 크게 작용합니다."
        ideas = [
            "필지 전체를 한 덩어리로 보지 말고 경계, 계곡부, 접근로 주변을 제외한 순관리면적을 먼저 산정합니다.",
            "인접 필지가 많으면 산주 공동 탄소관리 구역을 제안해 조사비와 장기 모니터링 비용을 나눌 수 있습니다.",
            "임도 접근이 가능한 구간에는 표본 조사구를 두고, 접근이 나쁜 내부 구간은 훼손을 줄이는 장기 보전 구역으로 분리합니다.",
        ]
        phases = [
            {"name": "1단계 기준선 확정", "actions": ["경계와 제외면적 산정", "수종과 영급 표본조사", "기존 벌채나 피해 이력 확인"]},
            {"name": "2단계 흡수량 설계", "actions": ["관리 대상 면적별 흡수량 예비계산", "등록 사례와 면적, 수종, 영급 비교", "장기 모니터링 동선 배치"]},
            {"name": "3단계 실행 방식", "actions": ["보식, 간벌, 방치 구역을 나누어 관리", "산주 공동관리 가능성 확인", "수익형 사업과 충돌하는 구역 제외"]},
        ]
        risks = [
            "영급이나 수종이 실제와 다르면 탄소형 점수는 바로 바뀝니다.",
            "접근성이 낮은 구간은 조사와 모니터링 비용이 커지므로 순관리면적을 줄이는 편이 현실적입니다.",
        ]
    elif top_name == "수익형":
        thesis = f"수익형이 앞선 이유는 조림 후보 {features.planting_fit_count or 0}건, 경제림 판정 {'포함' if features.economic_forest else '미포함'}, 접근성 {round(access)}점의 조합입니다. 재난위험 {round(disaster)}점이 낮을수록 작업 일정 선택 폭이 넓어집니다."
        ideas = [
            "임도와 가까운 구역부터 작업 블록을 나누고 운반비가 높은 내부 구역은 후순위로 둡니다.",
            "조림 후보 수종과 현재 임상을 비교해 모두 베는 방식보다 보식과 부분 갱신을 먼저 검토합니다.",
            "주변 필지가 많으면 작업로와 운반 일정을 묶어 사업비를 낮추는 공동 발주를 제안할 수 있습니다.",
        ]
        phases = [
            {"name": "1단계 생산 블록화", "actions": ["임도 접근 구간 표시", "작업 가능 면적 분리", "운반 거리별 비용 구간 작성"]},
            {"name": "2단계 수종 전략", "actions": ["현재 수종과 추천 수종 비교", "보식, 갱신, 숲가꾸기 구간 구분", "경제림 사업 조건 확인"]},
            {"name": "3단계 견적 검증", "actions": ["산림사업법인 2곳 이상 비교", "계절별 작업 제한 반영", "수익과 보조사업 조합 계산"]},
        ]
        risks = [
            "임도 데이터가 있어도 실제 통행이 막혀 있으면 수익형 점수가 과대평가됩니다.",
            "경제림 구역이 아니면 지자체 사업 대상 여부를 별도로 확인해야 합니다.",
        ]
    elif top_name == "보전형":
        thesis = f"보전형은 재난위험 {round(disaster)}점과 경사 {slope}가 숲의 훼손 가능성을 크게 만들 때 선택됩니다. 이 경우 수익보다 토양 안정과 물길 관리가 먼저입니다."
        ideas = [
            "급경사와 계곡부를 핵심 보전 구역으로 묶고, 임도 주변만 최소 작업 구역으로 둡니다.",
            "산주가 활용을 원하면 탐방로, 교육림, 생태 보전형 관리처럼 훼손이 적은 모델을 검토합니다.",
            "주변 필지가 많으면 물길이 이어지는 단위로 보전 협약을 제안할 수 있습니다.",
        ]
        phases = [
            {"name": "1단계 민감구역 표시", "actions": ["고위험 사면과 계곡부 분리", "작업 제한선 설정", "토양 침식 흔적 확인"]},
            {"name": "2단계 저강도 관리", "actions": ["고사목과 병해충 구간만 선별", "배수 흐름 유지", "훼손 구간 복원 우선순위 작성"]},
            {"name": "3단계 활용 모델", "actions": ["보전형 지원사업 확인", "인접 필지 연계 가능성 검토", "장기 모니터링 지점 지정"]},
        ]
        risks = [
            "보전형은 당장 수익이 낮아 보일 수 있어 산주의 목적을 먼저 확인해야 합니다.",
            "일부 안전 작업은 보전 목적이어도 허가와 전문가 검토가 필요합니다.",
        ]
    else:
        thesis = f"재난저감형은 재난위험 {round(disaster)}점과 접근성 {round(access)}점의 조합 때문에 선택됩니다. 접근이 어려운 위험 필지는 사고가 나기 전에 동선과 배수부터 정리해야 합니다."
        ideas = [
            "위험 구간을 전부 정비하기보다 임도, 계곡부, 사면 하단처럼 피해가 커지는 지점을 먼저 고릅니다.",
            "산불과 산사태 대응 동선을 같은 지도 위에 올려 긴급 진입로와 작업 제한 구역을 함께 설계합니다.",
            "주변 필지와 물길이 이어지면 단일 필지만 정비하는 것보다 공동 저감 사업이 합리적입니다.",
        ]
        phases = [
            {"name": "1단계 위험 위치 확정", "actions": ["산사태 격자와 경계 중첩 확인", "배수로와 절개지 점검", "임도 유실 가능 지점 표시"]},
            {"name": "2단계 계절 대응", "actions": ["장마 전 배수 작업", "건조기 산불 진입로 확보", "강풍일 작업 제한 기준 작성"]},
            {"name": "3단계 수행 주체", "actions": ["방제와 복구 가능 법인 후보 비교", "지자체 재난저감 사업 확인", "작업 후 재점검 일정 수립"]},
        ]
        risks = [
            "위험 점수가 낮아도 현장 배수 불량 하나가 전체 우선순위를 바꿀 수 있습니다.",
            "접근성이 낮으면 작업 비용보다 안전관리 계획이 먼저 정리돼야 합니다.",
        ]

    return {
        "scenario": top_name,
        "score": top_score,
        "secondScenario": second_name,
        "margin": margin,
        "thesis": thesis,
        "context": common_context,
        "ideas": ideas,
        "phases": phases,
        "risks": risks,
    }


def _scenario_score_map(scores: dict) -> dict[str, float]:
    return {
        "수익형": scores.get("profit", 0) or 0,
        "탄소형": scores.get("carbon", 0) or 0,
        "보전형": scores.get("conservation", 0) or 0,
        "재난저감": scores.get("resilience", 0) or 0,
    }


def _source_parcel_evidence(source_id: str, features: FeatureSet, scores: dict) -> list[str]:
    access = scores.get("accessibility", 0) or 0
    disaster = scores.get("disasterRisk", 0) or 0
    area = features.area_ha or 0
    age = features.stand_age_class
    road = _format_meters(features.road_distance_m)
    slope = _format_degree(features.slope_degree)
    landslide = _format_grade(features.avg_landslide_grade)
    density = _format_density(features.road_density_m_per_ha)
    data = {
        "D1": [
            f"영급은 {age if age is not None else '현장 확인 대상'}입니다. 영급이 확인되면 탄소형과 수익형 판단이 가장 크게 보정됩니다.",
            f"현재 탄소형 {round(scores.get('carbon', 0) or 0)}점은 면적 {round(area, 2)}ha와 확인된 임상 속성만 반영한 값입니다.",
            "수종과 수관밀도 원천 속성이 적재되면 장기 관리와 벌채 가능성 문장을 더 좁힐 수 있습니다.",
        ],
        "D2": [
            f"경사는 {slope}입니다. 값이 확인되면 접근성, 보전형, 재난저감형 판단이 함께 보정됩니다.",
            f"현재 접근성 {round(access)}점은 확인된 임도 정보와 경사 정보를 반영한 작업 동선 점수입니다.",
            "토심과 토성은 조림 후보 검토의 보정값으로 읽히며, 실제 장비 진입 전 현장 확인이 필요합니다.",
        ],
        "D3": [
            f"맞춤형 조림 후보가 {features.planting_fit_count or 0}건 교차되어 수익형 생산성 항목에 반영됐습니다.",
            f"후보 수가 많을수록 수종 선택지가 넓어지지만, 이 필지의 최종 추천은 {scores.get('recommendedScenario', '확인 필요')}입니다.",
            "후보 수종은 임상도 수종과 겹치는지 확인해야 실행 계획으로 넘어갈 수 있습니다.",
        ],
        "D4": [
            f"가까운 임도 거리는 {road}, 필지 내부 임도 밀도는 {density}입니다.",
            f"임도 거리가 확인되면 수익형의 운반비 판단과 재난저감형의 긴급 진입 동선 판단이 함께 좁혀집니다.",
            "임도 상태가 실제 차량 통행에 맞지 않으면 수익형 점수는 현장 보정에서 낮아질 수 있습니다.",
        ],
        "D5": [
            f"산사태 평균등급은 {landslide}입니다. 값이 비어 있으면 안전하다는 뜻이 아니라 위험 구간 산정 전 상태입니다.",
            f"재난위험 항목은 현재 {round(disaster)}점으로 표시되며, 산사태 격자와 산불위험 지수가 들어오면 재난저감형 판단이 보정됩니다.",
            "계곡부와 임도 절개지는 점수와 별개로 별도 구간으로 분리해서 봐야 합니다.",
        ],
        "D8": [
            "경제림 구역 포함 여부는 수익형 생산성 점수에 직접 반영됩니다.",
            f"현재 경제림 판정은 {'포함' if features.economic_forest else '미포함'}으로 계산됐습니다.",
            f"수익형은 {round(scores.get('profit', 0) or 0)}점이며, 접근성 {round(access)}점과 조림 후보 {features.planting_fit_count or 0}건이 같이 작용했습니다.",
        ],
        "D12": [
            f"필지 경계에서 면적 {round(area, 2)}ha가 계산됐고, 이 값이 탄소형과 재난저감형의 면적 보정에 들어갔습니다.",
            "같은 지번이라도 경계가 달라지면 임도 거리, 위험도, 임상 교차 결과가 모두 바뀝니다.",
            f"이번 분석 상태는 {scores.get('recommendedScenario', '확인 필요')}이며, 경계 정확도가 전체 판단의 출발점입니다.",
        ],
    }
    return data.get(source_id, [])


def _format_meters(value: float | None) -> str:
    if value is None:
        return "확인 필요"
    return f"{round(value):,}m"


def _format_degree(value: float | None) -> str:
    if value is None:
        return "확인 필요"
    return f"{round(value, 1)}도"


def _format_grade(value: float | None) -> str:
    if value is None:
        return "확인 필요"
    return f"{round(value, 2)}등급"


def _format_density(value: float | None) -> str:
    if value is None:
        return "확인 필요"
    return f"{round(value, 1)}m/ha"


def _gap_text(scores: dict) -> str:
    gaps = scores.get("dataGaps") or []
    if not gaps:
        return "핵심 근거"
    return ", ".join(gaps)


def _source_analysis_detail(source_id: str) -> dict:
    details = {
        "D1": {
            "role": "필지와 가장 넓게 겹치는 임상 구역을 대표값으로 잡아 현재 숲의 생육 단계와 구조를 읽습니다.",
            "extracted": [
                "영급은 탄소형 점수의 핵심 입력값으로 들어갑니다.",
                "임상과 주요 수종은 조림 후보, 장기 관리 방향, 벌채 가능성 검토에 연결됩니다.",
                "수관밀도와 경급은 실제 작업 전 현장 표본조사로 보정할 항목입니다.",
            ],
            "scoreImpact": [
                "탄소형: 영급이 높고 면적이 충분하면 장기 흡수 관리 가능성이 올라갑니다.",
                "수익형: 조림지도와 경제림 구역 결과와 함께 생산성 판단에 들어갑니다.",
                "보전형: 숲 구조가 안정적인 구역은 훼손을 줄이는 관리 방향을 우선 검토합니다.",
            ],
            "fieldCheck": [
                "경계부에 다른 수종이 섞인 경우 대표 임상값만으로 판단하지 않습니다.",
                "임상도 작성 시점과 현재 벌채, 피해, 조림 이력 차이를 현장에서 확인합니다.",
            ],
            "caveat": "임상도는 넓은 구역 단위 속성입니다. 필지 내부가 여러 임상으로 나뉘면 면적 비중을 추가로 비교해야 합니다.",
        },
        "D2": {
            "role": "토양과 지형 조건을 읽어 작업 난이도, 접근성 페널티, 보전 필요성을 계산합니다.",
            "extracted": [
                "경사도는 접근성 점수에서 작업 난이도 페널티로 반영됩니다.",
                "토심과 토성은 조림 적합성, 장비 진입 가능성, 배수 검토에 쓰입니다.",
                "사면 방향과 표고는 건조 위험과 수종 선택을 해석할 때 참고합니다.",
            ],
            "scoreImpact": [
                "접근성: 경사가 높으면 같은 임도 거리라도 작업 점수가 낮아집니다.",
                "보전형: 급경사와 얕은 토심은 훼손 최소화 판단을 강화합니다.",
                "재난저감: 토양 조건이 나쁘면 배수와 침식 관리를 우선 작업으로 올립니다.",
            ],
            "fieldCheck": [
                "실제 진입로, 계곡부, 암반 노출 여부를 지도 경사값과 대조합니다.",
                "작업 장비 투입 전 우천 후 토양 상태를 확인합니다.",
            ],
            "caveat": "토양도 속성은 구획 대표값입니다. 작은 계곡, 절토면, 암반 노출은 지도보다 현장 영향이 클 수 있습니다.",
        },
        "D3": {
            "role": "필지와 맞춤형조림지도 구역을 겹쳐 추천 수종 후보와 조림 가능성을 확인합니다.",
            "extracted": [
                "교차된 추천 수종 수를 생산성 판단의 보조 입력으로 사용합니다.",
                "입지 적합 후보가 많으면 수익형과 탄소형 모두 선택지가 넓어집니다.",
                "후보 수종은 임상도 수종과 맞춰 기존 숲 유지, 갱신, 보식 방향을 나눕니다.",
            ],
            "scoreImpact": [
                "수익형: 조림 후보가 많을수록 사업 설계 가능성이 커집니다.",
                "탄소형: 장기 생장에 맞는 수종 후보가 있으면 탄소 관리 시나리오가 안정됩니다.",
            ],
            "fieldCheck": [
                "추천 수종이 실제 토심, 배수, 주변 병해충 조건과 맞는지 확인합니다.",
                "조림 가능 면적은 소유 경계, 묘지, 도로, 계류부를 제외하고 다시 계산합니다.",
            ],
            "caveat": "추천 수종은 입지 기반 후보입니다. 사업 실행 전 산림기술자 설계와 지역 보급 묘목 상황을 함께 봐야 합니다.",
        },
        "D4": {
            "role": "가까운 임도까지의 거리와 필지 내부 임도 밀도를 계산해 작업 동선과 운반성을 평가합니다.",
            "extracted": [
                "가장 가까운 임도 거리는 접근성 점수의 중심 입력입니다.",
                "필지와 겹치는 임도 길이는 면적당 임도 밀도로 환산합니다.",
                "임도 접근성이 낮으면 재난 대응과 작업비 부담이 같이 올라갑니다.",
            ],
            "scoreImpact": [
                "접근성: 임도 거리가 짧고 밀도가 높을수록 점수가 올라갑니다.",
                "수익형: 운반 거리가 짧으면 생산 작업 가능성이 좋아집니다.",
                "재난저감: 접근성이 낮은 위험 필지는 사전 점검 작업을 앞에 둡니다.",
            ],
            "fieldCheck": [
                "지도상 임도가 실제 차량 진입 가능한 상태인지 확인합니다.",
                "사유지 통행, 임도 훼손, 계절별 통제 여부를 별도로 확인합니다.",
            ],
            "caveat": "임도망도는 선형 데이터입니다. 포장 상태, 경사, 차단기, 붕괴 지점은 현장 조사로 보정해야 합니다.",
        },
        "D5": {
            "role": "산사태위험 격자를 필지 경계로 잘라 평균 위험도와 위험 구간을 계산합니다.",
            "extracted": [
                "위험등급 평균은 재난위험 점수의 중심 입력입니다.",
                "고위험 격자 비율은 재난저감형 판단을 강화하는 보조 입력입니다.",
                "위험 구간은 올해 작업에서 배수, 사면 점검, 진입 제한 위치를 잡을 때 씁니다.",
            ],
            "scoreImpact": [
                "재난위험: 평균 위험등급과 고위험 비율이 높을수록 점수가 올라갑니다.",
                "보전형: 위험이 높은 급경사 필지는 훼손을 줄이는 방향으로 기웁니다.",
                "재난저감: 산사태 위험이 높고 접근성이 낮으면 사전 점검 우선도가 올라갑니다.",
            ],
            "fieldCheck": [
                "계곡부, 배수로 막힘, 절개지, 최근 벌채지를 현장에서 먼저 확인합니다.",
                "장마 전후 작업 일정은 산악기상과 함께 다시 판단합니다.",
            ],
            "caveat": "격자 위험도는 지형 기반 예측값입니다. 최근 공사, 임도 유실, 배수 불량은 별도 확인이 필요합니다.",
        },
        "D8": {
            "role": "경제림육성단지 포함 여부를 확인해 생산 중심 경영을 검토할 수 있는지 판단합니다.",
            "extracted": [
                "경제림 구역 포함 여부를 수익형 점수에 반영합니다.",
                "단지 정보는 숲가꾸기, 조림, 벌채 설계의 행정 검토 기준으로 참고합니다.",
                "경제림이 아니어도 임상, 접근성, 면적 조건이 좋으면 다른 시나리오를 비교합니다.",
            ],
            "scoreImpact": [
                "수익형: 경제림 구역이면 생산과 관리 사업 검토 우선도가 올라갑니다.",
                "탄소형: 경제림 여부보다 장기 생장과 면적 조건이 더 크게 작용할 수 있습니다.",
            ],
            "fieldCheck": [
                "해당 구역의 지자체 사업 공고, 보조사업 대상 조건, 산주 동의 요건을 확인합니다.",
                "경제림 경계와 필지 경계가 일부만 겹치면 실제 대상 면적을 다시 산정합니다.",
            ],
            "caveat": "경제림 구역은 사업 가능성을 높이는 근거이지 허가나 지원 확정을 뜻하지 않습니다.",
        },
        "D12": {
            "role": "주소, 지번, 지도 클릭 위치를 필지 경계와 PNU로 바꿔 모든 공간분석의 기준 경계를 만듭니다.",
            "extracted": [
                "PNU는 필지 식별값으로 저장하고 조회 기준으로 사용합니다.",
                "GeoJSON 경계는 면적 계산과 모든 산림공간정보 교차의 기준입니다.",
                "행정구역과 주소는 산불위험, 법인 후보, 통계 조회 범위를 좁히는 데 사용합니다.",
            ],
            "scoreImpact": [
                "모든 점수: 경계가 바뀌면 면적, 임도 거리, 토양, 임상, 위험도가 모두 바뀝니다.",
                "탄소형: 면적값은 장기 흡수 관리 가능성 계산에 직접 들어갑니다.",
            ],
            "fieldCheck": [
                "공유지, 임야대장, 실제 점유 경계가 지적 경계와 맞는지 확인합니다.",
                "산 번지와 일반 번지가 섞인 지역은 PNU와 주소를 다시 확인합니다.",
            ],
            "caveat": "연속지적도는 분석 기준 경계입니다. 소유권, 이용 제한, 인허가 판단은 공부와 담당 기관 확인이 필요합니다.",
        },
    }
    return details.get(
        source_id,
        {
            "role": "분석 과정에서 참조하는 공공데이터입니다.",
            "extracted": [],
            "scoreImpact": [],
            "fieldCheck": [],
            "caveat": "원천 데이터 갱신일과 제공기관 기준을 확인해야 합니다.",
        },
    )


def _build_xai(features: FeatureSet, scores: dict) -> dict:
    road_score = round(distance_score(features.road_distance_m), 1)
    slope_cost = round(slope_penalty(features.slope_degree), 1)
    road_bonus = round(min((features.road_density_m_per_ha or 0) * 8, 18), 1)
    area_bonus = round(min((features.area_ha or 0) * 1.6, 14), 1)
    carbon_age_bonus = round(min((features.stand_age_class or 0) * 3.5, 22), 1)
    resilience_area_bonus = round(min((features.area_ha or 0) * 2, 18), 1)
    access_interpretation = (
        f"임도 거리 {_format_meters(features.road_distance_m)}에서 {road_score}점, 경사 {_format_degree(features.slope_degree)}에서 감점 {slope_cost}점, 임도 밀도 보정 {road_bonus}점이 반영되어 접근성은 {round(scores.get('accessibility', 0) or 0)}점입니다."
        if features.road_distance_m is not None and features.slope_degree is not None
        else f"접근성은 임도 거리와 경사가 핵심인데 현재 {_gap_text(scores)} 확보가 먼저입니다. 지금 값은 작업로 판단 전 예비값이며, 임도 실거리와 장비 진입 가능 지점이 들어오면 수익형 점수가 크게 바뀝니다."
    )
    disaster_interpretation = (
        f"산사태 평균등급 {_format_grade(features.avg_landslide_grade)}와 산불위험 지수 {_format_grade(features.fire_risk_index)}가 반영되어 재난위험은 {round(scores.get('disasterRisk', 0) or 0)}점입니다."
        if features.avg_landslide_grade is not None or features.fire_risk_index is not None
        else "산사태와 산불위험 값이 아직 비어 있습니다. 이 상태는 안전 판정이 아니며, 계곡부·사면 하단·임도 절개지를 먼저 확인해야 합니다."
    )
    carbon_interpretation = (
        f"면적 {round(features.area_ha or 0, 2)}ha가 {area_bonus}점, 영급 {features.stand_age_class}이 {carbon_age_bonus}점 기여했습니다. 접근성과 재난위험이 좋지 않으면 장기 관리 비용을 반영해 감점됩니다."
        if features.stand_age_class is not None
        else f"면적 {round(features.area_ha or 0, 2)}ha는 반영됐지만 영급과 수종이 아직 비어 있습니다. 탄소형 {round(scores.get('carbon', 0) or 0)}점은 등록 가능성 확정이 아니라 기준선 산정 전 후보값입니다."
    )
    return {
        "method": "공공데이터 검색 체인과 가중치 기반 설명",
        "retrievalChain": [
            {"step": "필지 확정", "sourceIds": ["D12"], "evidence": "연속지적도 경계 또는 VWorld 조회 geometry를 분석 기준으로 사용"},
            {"step": "공간 교차", "sourceIds": ["D1", "D2", "D3", "D4", "D5", "D8"], "evidence": "필지와 산림공간정보를 PostGIS에서 교차"},
            {"step": "지표 산정", "sourceIds": ["D2", "D4", "D5", "D8"], "evidence": "접근성, 재난위험, 생산성, 보전성 지표 계산"},
            {"step": "경영 방향", "sourceIds": ["D1", "D3", "D9", "D10", "D11"], "evidence": "시나리오 점수와 실행 작업 후보 연결"},
        ],
        "scoreExplanations": [
            {
                "metric": "접근성",
                "score": scores.get("accessibility"),
                "formula": "임도 거리 점수 - 경사 페널티 + 임도 밀도 보정",
                "sourceIds": ["D2", "D4"],
                "inputs": {
                    "roadDistanceM": features.road_distance_m,
                    "roadDensityMPerHa": features.road_density_m_per_ha,
                    "slopeDegree": features.slope_degree,
                },
                "interpretation": access_interpretation,
            },
            {
                "metric": "재난위험",
                "score": scores.get("disasterRisk"),
                "formula": "산사태 평균등급과 고위험 비율을 위험 점수로 환산",
                "sourceIds": ["D5", "D6"],
                "inputs": {
                    "avgLandslideGrade": features.avg_landslide_grade,
                    "highLandslideRatio": features.high_landslide_ratio,
                    "fireRiskIndex": features.fire_risk_index,
                },
                "interpretation": disaster_interpretation,
            },
            {
                "metric": "수익형",
                "score": scores.get("profit"),
                "formula": "생산성 50% + 접근성 38% + 재난 안정성 12%",
                "sourceIds": ["D1", "D3", "D4", "D8"],
                "inputs": {
                    "economicForest": features.economic_forest,
                    "plantingFitCount": features.planting_fit_count,
                    "standAgeClass": features.stand_age_class,
                },
                "interpretation": f"경제림 판정은 {'포함' if features.economic_forest else '미포함'}이고 조림 후보는 {features.planting_fit_count or 0}건입니다. 수익형 {round(scores.get('profit', 0) or 0)}점은 생산 조건과 작업 동선을 함께 본 값이며, 임도와 경사가 비어 있으면 사업비 판단 전 후보값으로 봅니다.",
            },
            {
                "metric": "탄소형",
                "score": scores.get("carbon"),
                "formula": "기본점 + 영급 보정 + 면적 보정 + 위험·접근 보정",
                "sourceIds": ["D1", "D9"],
                "inputs": {
                    "areaHa": features.area_ha,
                    "standAgeClass": features.stand_age_class,
                    "carbonCaseSimilarity": features.carbon_case_similarity,
                },
                "interpretation": carbon_interpretation,
            },
            {
                "metric": "보전형",
                "score": scores.get("conservation"),
                "formula": "재난위험 58% + 경사 페널티",
                "sourceIds": ["D2", "D5"],
                "inputs": {
                    "slopeDegree": features.slope_degree,
                    "avgLandslideGrade": features.avg_landslide_grade,
                },
                "interpretation": f"보전형 {round(scores.get('conservation', 0) or 0)}점은 경사와 재난위험이 확인될수록 의미가 선명해집니다. 현재 경사 {_format_degree(features.slope_degree)}, 산사태 {_format_grade(features.avg_landslide_grade)} 상태이므로 계곡부와 급경사 경계 확인을 먼저 둡니다.",
            },
            {
                "metric": "재난저감형",
                "score": scores.get("resilience"),
                "formula": "재난위험 62% + 낮은 접근성 보정 + 면적 보정",
                "sourceIds": ["D4", "D5", "D6"],
                "inputs": {
                    "areaHa": features.area_ha,
                    "roadDistanceM": features.road_distance_m,
                    "avgLandslideGrade": features.avg_landslide_grade,
                },
                "interpretation": f"재난저감형 {round(scores.get('resilience', 0) or 0)}점은 면적 보정 {resilience_area_bonus}점과 접근 동선, 위험 지표를 함께 봅니다. 위험값이 비어 있으면 배수와 임도 유실 확인을 먼저 배치합니다.",
            },
        ],
    }
