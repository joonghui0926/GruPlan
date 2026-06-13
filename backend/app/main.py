from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .data_catalog import PUBLIC_DATA_SOURCES, SOURCE_BY_ID
from .db import Database
from .public_clients import PublicApiClient, PublicDataError
from .reports import build_plan_pdf
from .scoring import FeatureSet, score_features
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
            status = "적재 완료" if source.table_name in loaded_tables else "원본 적재 필요"
        if source.requires_key:
            key_ready = configured_keys["vworld"] if source.id == "D12" else configured_keys["data"]
            status = "API 키 필요" if not key_ready else status
        item = source.to_dict()
        item["status"] = status
        items.append(item)
    return {"items": items}


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
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc), "sourceId": exc.source_id}) from exc
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
        "features": row["features"],
        "scores": scores,
        "workPlan": _work_plan(scores),
        "sources": _analysis_sources(),
    }


@app.get("/api/live/mountain-weather")
async def mountain_weather(obsid: str | None = None, localArea: str | None = None):
    try:
        return await public_client.mountain_weather(obsid=obsid, local_area=localArea)
    except PublicDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc), "sourceId": exc.source_id}) from exc


@app.get("/api/live/fire-risk")
async def fire_risk(sigunguCode: str | None = None):
    try:
        return await public_client.fire_risk(sigunguCode=sigunguCode)
    except PublicDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc), "sourceId": exc.source_id}) from exc


@app.get("/api/forest-companies")
async def forest_companies(tradeName: str | None = None, captain: str | None = None):
    try:
        return await public_client.forest_companies(trade_name=tradeName, captain=captain)
    except PublicDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc), "sourceId": exc.source_id}) from exc


@app.get("/api/economic-forest")
async def economic_forest(search: str | None = None, frstType: str | None = None):
    try:
        return await public_client.economic_forest(search=search, frst_type=frstType)
    except PublicDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc), "sourceId": exc.source_id}) from exc


@app.get("/api/resource-stats")
async def resource_stats(classId: str | None = None):
    try:
        return await public_client.resource_stats(class_id=classId)
    except PublicDataError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"message": str(exc), "sourceId": exc.source_id}) from exc


@app.post("/api/reports/plan")
async def plan_report(payload: ReportRequest, _: BackgroundTasks):
    pdf = build_plan_pdf(payload.analysis)
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
      select null::text as pnu, null::text as address, null::text as admin_name,
             ST_SetSRID(ST_GeomFromGeoJSON($2), 4326) as geom
      where $1::text is null and $2::text is not null
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
        'stand', stand.properties
      ) as features
    from area_calc a
    cross join nearest_road nr
    cross join road_density rd
    cross join planting
    cross join economic
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


def _first_vworld_point(search: dict) -> dict | None:
    items = search.get("response", {}).get("result", {}).get("items", [])
    if not items:
        return None
    point = items[0].get("point") or {}
    try:
        return {"lon": float(point["x"]), "lat": float(point["y"])}
    except (KeyError, TypeError, ValueError):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _work_plan(scores: dict) -> list[dict]:
    scenario = scores.get("recommendedScenario")
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


def _analysis_sources() -> list[dict]:
    return [
        {"id": source.id, "name": source.name, "status": "분석 근거"}
        for source in PUBLIC_DATA_SOURCES
        if source.id in {"D1", "D2", "D3", "D4", "D5", "D8", "D12"}
    ]
