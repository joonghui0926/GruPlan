from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PublicDataSource:
    id: str
    name: str
    provider: str
    kind: str
    access: str
    url: str
    usage: str
    table_name: str | None
    requires_key: bool
    ingestion: str
    license_note: str = "공공데이터포털 또는 제공기관 이용조건 준수"

    def to_dict(self) -> dict:
        return asdict(self)


PUBLIC_DATA_SOURCES: tuple[PublicDataSource, ...] = (
    PublicDataSource(
        id="D1",
        name="산림청_임상도 산림공간정보",
        provider="산림청",
        kind="SHP",
        access="기관 제공 파일",
        url="https://www.data.go.kr/data/3045619/fileData.do",
        usage="임종, 임상, 주요 수종, 수관밀도, 영급, 경급을 필지와 교차해 현재 산림 상태를 판단",
        table_name="forest_stands",
        requires_key=False,
        ingestion="원본 SHP를 내려받아 ogr2ogr로 PostGIS에 적재",
    ),
    PublicDataSource(
        id="D2",
        name="산림청_산림입지토양도 산림공간정보",
        provider="산림청",
        kind="SHP",
        access="기관 제공 파일",
        url="https://www.data.go.kr/data/3058167/fileData.do",
        usage="지형, 모암, 표고, 경사, 사면, 토심, 토성, 토양형으로 생산성과 작업 난이도 계산",
        table_name="forest_soils",
        requires_key=False,
        ingestion="원본 SHP를 내려받아 ogr2ogr로 PostGIS에 적재",
    ),
    PublicDataSource(
        id="D3",
        name="산림청_맞춤형조림지도 산림공간정보",
        provider="산림청",
        kind="SHP",
        access="기관 제공 파일",
        url="https://www.data.go.kr/data/3045620/fileData.do",
        usage="입지와 기후 기반 추천 수종을 경영 시나리오의 수종 후보로 사용",
        table_name="planting_zones",
        requires_key=False,
        ingestion="원본 SHP를 내려받아 ogr2ogr로 PostGIS에 적재",
    ),
    PublicDataSource(
        id="D4",
        name="산림청_임도망도 산림공간정보",
        provider="산림청",
        kind="SHP",
        access="기관 제공 파일",
        url="https://www.data.go.kr/data/3045621/fileData.do",
        usage="가까운 임도까지의 거리와 임도 밀도를 계산해 접근성과 운반 난이도 산정",
        table_name="forest_roads",
        requires_key=False,
        ingestion="원본 SHP를 내려받아 ogr2ogr로 PostGIS에 적재",
    ),
    PublicDataSource(
        id="D5",
        name="산림청_산사태위험지도",
        provider="산림청",
        kind="Raster",
        access="산림공간정보서비스 신청 또는 예시 데이터",
        url="https://www.forest.go.kr/kfsweb/kfi/kfs/trail/sanSaTae.do?pblicDataId=PBD0000210&tabs=4&mn=NKFS_06_08_02",
        usage="10m 격자 위험등급을 필지 단위로 집계해 보전형과 재난저감형 점수에 반영",
        table_name="landslide_risk",
        requires_key=False,
        ingestion="원본 IMG 또는 GeoTIFF를 raster2pgsql로 PostGIS Raster에 적재",
    ),
    PublicDataSource(
        id="D6",
        name="산림청 국립산림과학원_산불위험예보정보",
        provider="산림청 국립산림과학원",
        kind="REST API",
        access="공공데이터포털 OpenAPI",
        url="https://www.data.go.kr/data/15084817/openapi.do",
        usage="시군구 단위 산불위험 예보를 단기 작업 가능일과 산불관리 가이드에 반영",
        table_name=None,
        requires_key=True,
        ingestion="DATA_GO_KR_SERVICE_KEY로 실시간 조회 후 분석 결과에 캐시",
    ),
    PublicDataSource(
        id="D7",
        name="산림청 국립산림과학원_산악기상정보",
        provider="산림청 국립산림과학원",
        kind="REST API",
        access="공공데이터포털 OpenAPI",
        url="https://www.data.go.kr/data/15084696/openapi.do",
        usage="풍향, 풍속, 온도, 습도, 강수량, 기압, 지면온도로 작업 위험 판단",
        table_name=None,
        requires_key=True,
        ingestion="DATA_GO_KR_SERVICE_KEY로 실시간 조회 후 분석 결과에 캐시",
    ),
    PublicDataSource(
        id="D8",
        name="산림청_경제림육성단지구역도",
        provider="산림청",
        kind="OpenAPI XML",
        access="공공데이터포털 OpenAPI",
        url="https://www.data.go.kr/data/3070922/openapi.do",
        usage="경제림 구역 여부와 관리 단지 정보를 수익형 경영 가능성 점수에 반영",
        table_name="economic_forest_zones",
        requires_key=True,
        ingestion="OpenAPI 조회 또는 GW 공간데이터를 PostGIS에 적재",
    ),
    PublicDataSource(
        id="D9",
        name="산림청_산림탄소상쇄사업 등록 현황",
        provider="산림청",
        kind="CSV JSON XML OpenAPI",
        access="공공데이터포털 파일데이터 및 자동 변환 OpenAPI",
        url="https://www.data.go.kr/data/15125368/fileData.do",
        usage="사업유형, 면적, 탄소흡수량, 사업내용으로 탄소형 경영 체크리스트 구성",
        table_name="carbon_offset_projects",
        requires_key=True,
        ingestion="공공데이터포털 자동 변환 OpenAPI로 조회하고 CSV 원본 적재 시 보강",
    ),
    PublicDataSource(
        id="D10",
        name="산림청_산림사업법인 정보",
        provider="산림청",
        kind="OpenAPI XML",
        access="공공데이터포털 OpenAPI",
        url="https://www.data.go.kr/data/3071214/openapi.do",
        usage="지역, 사업종류, 기술 인력으로 숲가꾸기, 방제, 임도, 복원 수행 후보 추천",
        table_name="forest_business_companies",
        requires_key=True,
        ingestion="DATA_GO_KR_SERVICE_KEY로 실시간 조회 후 상담 후보에 캐시",
    ),
    PublicDataSource(
        id="D11",
        name="산림청_산림자원통계 서비스",
        provider="산림청",
        kind="OpenAPI JSON",
        access="공공데이터포털 OpenAPI",
        url="https://www.data.go.kr/data/15080832/openapi.do",
        usage="전국산주현황, 임산물생산조사, 임가경제조사 등을 시장성 분석에 활용",
        table_name="forest_resource_stats",
        requires_key=True,
        ingestion="DATA_GO_KR_SERVICE_KEY로 통계 목록과 지표를 주기 동기화",
    ),
    PublicDataSource(
        id="D12",
        name="국토교통부_연속지적도",
        provider="국토교통부",
        kind="JSON XML WMS WFS",
        access="VWorld 연계 OpenAPI",
        url="https://www.data.go.kr/data/15056910/openapi.do",
        usage="주소, 지번, 지도 클릭 좌표에서 필지 경계와 PNU를 추출해 공간분석의 기준으로 사용",
        table_name="parcels",
        requires_key=True,
        ingestion="VWORLD_API_KEY로 검색하거나 행정구역 단위 원본을 PostGIS에 적재",
    ),
)


SOURCE_BY_ID = {source.id: source for source in PUBLIC_DATA_SOURCES}
