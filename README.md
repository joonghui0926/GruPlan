# GruPlan

산림 공공데이터를 필지 단위 의사결정으로 연결하는 산림자산 경영 플랫폼입니다. 지번이나 지도 클릭으로 필지를 선택하고, 산림 상태 진단, 경영 시나리오, 작업 체크리스트, 산림경영계획서 초안을 확인할 수 있습니다.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/joonghui0926/GruPlan)

## 실행

```powershell
Copy-Item .env.example .env
docker compose up --build
```

브라우저에서 `http://localhost:8000`을 엽니다.

Docker 없이 실행할 때는 Python 3.10 가상환경을 사용합니다.

```powershell
py -3.10 -m venv .venv310
.\.venv310\Scripts\python.exe -m pip install -r .\backend\requirements.txt
Set-Location backend
..\.venv310\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 데이터 연결

분석 결과는 연결된 공공데이터를 기준으로 생성됩니다. `DATA_GO_KR_SERVICE_KEY`, `VWORLD_API_KEY`, `DATABASE_URL`을 설정하고 원본 공간데이터를 적재하면 필지 분석 흐름이 활성화됩니다. 보고서 문장 생성에는 `OPENAI_API_KEY`를 사용할 수 있으며, 기본 모델은 비용과 품질 균형을 고려해 `gpt-5.4-mini`로 설정되어 있습니다.

공간데이터 적재 예시는 아래와 같습니다.

```powershell
python backend/scripts/ingest_spatial.py --source D1 --file data/raw/forest_stands/sample.shp --source-srs EPSG:5179
python backend/scripts/ingest_spatial.py --source D5 --file data/raw/landslide/LDM_50.img --source-srs EPSG:5186
```

공공데이터 출처 메타데이터는 DB에 동기화할 수 있습니다.

```powershell
python backend/scripts/sync_public_sources.py --database-url $env:DATABASE_URL
```

## Render 환경변수

Render Web Service에는 아래 값을 넣습니다. 키 값은 저장소에 넣지 않고 Render 환경변수에만 저장합니다.

```text
DATA_GO_KR_SERVICE_KEY=공공데이터포털 Decoding 인증키
VWORLD_API_KEY=VWorld 운영 인증키
OPENAI_API_KEY=OpenAI Project API Key
OPENAI_MODEL=gpt-5.4-mini
FIRE_RISK_ENDPOINT=
CORS_ORIGINS=*
```

## 주요 기능

필지 선택: 주소, 지번, 지도 클릭으로 필지 경계와 PNU를 확인합니다.

산림자산 진단: 임상, 토양, 임도, 재난 위험, 경제림 구역 데이터를 결합합니다.

경영 시나리오: 수익형, 탄소형, 보전형, 재난저감형 방향을 비교합니다.

수행업체 탐색: 산림사업법인 정보를 조회해 상담 후보를 확인합니다.

보고서 생성: 진단 결과와 근거 데이터를 바탕으로 계획서 초안 PDF를 생성합니다.
