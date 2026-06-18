from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .data_catalog import PUBLIC_DATA_SOURCES, SOURCE_BY_ID
from .db import Database
from .llm import generate_plan_narrative
from .public_clients import PublicApiClient, PublicDataError, _current_admin_code, _forest_company_snapshot
from .reports import build_plan_pdf
from .scoring import FeatureSet, distance_score, score_features, slope_penalty
from .settings import get_settings


settings = get_settings()
db = Database(settings)
public_client = PublicApiClient(settings)
logger = logging.getLogger(__name__)


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
    client_features: dict | None = Field(default=None, alias="clientFeatures")


class ReportRequest(BaseModel):
    analysis: dict


class WorkRequestPayload(BaseModel):
    analysis: dict
    work_type: str | None = Field(default=None, alias="workType")
    status: str = "견적 요청"


class EmailAuthPayload(BaseModel):
    email: str
    password: str
    display_name: str | None = None
    next: str | None = "/#account"


class UserParcelPayload(BaseModel):
    analysis: dict | None = None
    parcel: dict | None = None
    note: str | None = None


class AnalysisRecordPayload(BaseModel):
    analysis: dict
    title: str | None = None


class WorkTaskPayload(BaseModel):
    title: str
    category: str = "현장 확인"
    status: str = "대기"
    due_date: str | None = Field(default=None, alias="dueDate")
    note: str | None = None
    pnu: str | None = None
    user_parcel_id: str | None = Field(default=None, alias="userParcelId")


class WorkTaskUpdatePayload(BaseModel):
    title: str | None = None
    category: str | None = None
    status: str | None = None
    due_date: str | None = Field(default=None, alias="dueDate")
    note: str | None = None


class FieldNotePayload(BaseModel):
    note: str
    pnu: str | None = None
    user_parcel_id: str | None = Field(default=None, alias="userParcelId")
    lat: float | None = None
    lon: float | None = None
    attachments: list[dict] = Field(default_factory=list)


class UserDocumentPayload(BaseModel):
    name: str
    kind: str = "분석 문서"
    source: str | None = None
    payload: dict = Field(default_factory=dict)
    pnu: str | None = None
    user_parcel_id: str | None = Field(default=None, alias="userParcelId")


class SharePayload(BaseModel):
    pnu: str | None = None
    user_parcel_id: str | None = Field(default=None, alias="userParcelId")
    permission: str = "view"
    expires_at: str | None = Field(default=None, alias="expiresAt")


STAND_AGE_KEYS = [
    "age_class",
    "age",
    "agcls_cd",
    "agcls_nm",
    "agcls",
    "영급",
    "영급코드",
    "영급명",
    "임령급",
    "나무나이",
    "수령",
    "agecls",
    "AGE_CLASS",
    "AGE",
    "AGCLS_CD",
    "AGCLS_NM",
    "AGCLS",
    "AGECLS_CD",
    "AGECLS_NM",
    "FRSTAGE",
    "FRST_AGE",
    "STORUNST_CD",
]
STAND_SPECIES_KEYS = [
    "species",
    "tree_species",
    "tree_species_name",
    "koftr_group",
    "koftr_group_cd",
    "koftr_group_nm",
    "koftr_cd",
    "koftr_nm",
    "frtp_cd",
    "frtp_nm",
    "sptree",
    "sptree_nm",
    "forest_type",
    "수종",
    "대표수종",
    "주요수종",
    "수종명",
    "주요수종명",
    "임상",
    "임종",
    "KOFTR_GROUP",
    "KOFTR_GROUP_CD",
    "KOFTR_GROUP_NM",
    "KOFTR_CD",
    "KOFTR_NM",
    "FRTP_CD",
    "FRTP_NM",
    "KOFTR",
    "KOFTR_CD1",
    "KOFTR_NM1",
]
SLOPE_KEYS = [
    "slope_degree",
    "slope",
    "slope_deg",
    "slant",
    "slant_cd",
    "slant_typ",
    "slant_type",
    "경사",
    "경사도",
    "평균경사",
    "경사등급",
    "SLOPE",
    "SLOPE_DEG",
    "SLANT",
    "SLANT_CD",
    "SLANT_TYP",
    "SLANT_TYPE",
]

FGIS_ENDPOINT = "https://map.forest.go.kr/gis1/iserver/services/data-fdms/rest/data/featureResults.json?returnContent=true"
FGIS_LAYER_CONFIG = {
    "stand": ("TB_FGDI_IM5000", "forest_stands"),
    "planting5000": ("TB_FGDI_FS_JJ5000", "planting_zones"),
    "planting25000": ("TB_FGDI_FS_JJ101", "planting_zones"),
    "soilFgis": ("TB_FGDI_FS_IJ100", "forest_soils"),
    "landslideVector": ("TB_FGIS_FS_FD100", None),
    "economicNational": ("TB_FGDI_C_FS_EN100", "economic_forest_zones"),
    "economicPrivate": ("TB_FGDI_C_FS_EN200", "economic_forest_zones"),
}
FGIS_LIVE_SOURCE_IDS = {"D1", "D2", "D3", "D5", "D8"}
CSV_FILE_SOURCE_IDS = {"D9"}
LIVE_OPEN_API_SOURCE_IDS = {"D11"}
SNAPSHOT_SOURCE_IDS = {"D10"}
SESSION_COOKIE_NAME = "gruplan_session"
SESSION_DAYS = 30
PASSWORD_ITERATIONS = 210_000
REGION_CODE_HINTS = {
    "춘천시": "51110",
    "원주시": "51130",
    "강릉시": "51150",
    "동해시": "51170",
    "태백시": "51190",
    "속초시": "51210",
    "삼척시": "51230",
    "홍천군": "51720",
    "횡성군": "51730",
    "영월군": "51750",
    "평창군": "51760",
    "정선군": "51770",
    "철원군": "51780",
    "화천군": "51790",
    "양구군": "51800",
    "인제군": "51810",
    "고성군": "51820",
    "양양군": "51830",
}


def _public_error(exc: PublicDataError) -> dict:
    return {"error": {"message": str(exc), "sourceId": exc.source_id}}


def _auth_providers_configured() -> dict:
    return {"email": True}


def _safe_next_path(value: str | None) -> str:
    text = str(value or "/").strip() or "/"
    if text.startswith("http://") or text.startswith("https://") or text.startswith("//"):
        return "/"
    return text if text.startswith("/") else f"/{text}"


def _b64_url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def _session_secret() -> bytes:
    return settings.session_secret.encode("utf-8")


def _signed_state(payload: dict) -> str:
    body = _b64_url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64_url(signature)}"


def _verify_state(value: str, provider: str) -> dict | None:
    try:
        body, signature = value.split(".", 1)
        expected = _b64_url(hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64_decode(body).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("provider") != provider:
        return None
    if int(time.time()) - int(payload.get("iat") or 0) > 600:
        return None
    return payload


def _auth_redirect_uri(provider: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/auth/{provider}/callback"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="이메일 형식이 맞지 않습니다.")
    return email


def _hash_password(password: str) -> str:
    if len(password or "") < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상으로 입력해주세요.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64_url(salt)}${_b64_url(digest)}"


def _verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, iterations, salt_text, digest_text = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = _b64_decode(salt_text)
        expected = _b64_decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def _create_session(user) -> str:
    raw_token = secrets.token_urlsafe(36)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await db.execute(
        """
        insert into user_sessions (token_hash, user_id, expires_at)
        values ($1, $2, $3)
        """,
        _hash_token(raw_token),
        user["id"],
        expires_at,
    )
    return raw_token


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        expires=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=settings.app_base_url.startswith("https://"),
        samesite="lax",
        path="/",
    )


async def _email_auth_response(user, next_path: str | None = "/#account") -> Response:
    raw_token = await _create_session(user)
    body = {
        "user": _user_payload(user),
        "next": _safe_next_path(next_path),
    }
    response = Response(json.dumps(body, ensure_ascii=False), media_type="application/json")
    _set_session_cookie(response, raw_token)
    return response


async def _google_profile(code: str) -> dict:
    token = await _oauth_token(
        "https://oauth2.googleapis.com/token",
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _auth_redirect_uri("google"),
        },
    )
    async with httpx.AsyncClient(timeout=18) as client:
        response = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        response.raise_for_status()
        info = response.json()
    return {
        "provider": "google",
        "providerUserId": str(info.get("sub")),
        "email": info.get("email"),
        "displayName": info.get("name") or info.get("email"),
        "avatarUrl": info.get("picture"),
        "profile": info,
    }


async def _kakao_profile(code: str) -> dict:
    payload = {
        "client_id": settings.kakao_client_id,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": _auth_redirect_uri("kakao"),
    }
    if settings.kakao_client_secret:
        payload["client_secret"] = settings.kakao_client_secret
    token = await _oauth_token("https://kauth.kakao.com/oauth/token", payload)
    async with httpx.AsyncClient(timeout=18) as client:
        response = await client.get(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        response.raise_for_status()
        info = response.json()
    account = info.get("kakao_account") if isinstance(info.get("kakao_account"), dict) else {}
    profile = account.get("profile") if isinstance(account.get("profile"), dict) else {}
    return {
        "provider": "kakao",
        "providerUserId": str(info.get("id")),
        "email": account.get("email"),
        "displayName": profile.get("nickname") or account.get("name") or "Kakao 사용자",
        "avatarUrl": profile.get("profile_image_url") or profile.get("thumbnail_image_url"),
        "profile": info,
    }


async def _oauth_token(url: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=18) as client:
        response = await client.post(
            url,
            data={key: value for key, value in payload.items() if value},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    if "access_token" not in data:
        raise HTTPException(status_code=502, detail="로그인 토큰을 받지 못했습니다.")
    return data


async def _upsert_oauth_user(profile: dict):
    if not db.pool:
        raise HTTPException(status_code=503, detail="DATABASE_URL이 필요합니다.")
    return await db.fetchrow(
        """
        insert into app_users (provider, provider_user_id, email, display_name, avatar_url, profile)
        values ($1, $2, $3, $4, $5, $6::jsonb)
        on conflict (provider, provider_user_id) do update set
          email = excluded.email,
          display_name = excluded.display_name,
          avatar_url = excluded.avatar_url,
          profile = excluded.profile,
          last_login_at = now()
        returning id, provider, provider_user_id, email, display_name, avatar_url, created_at, last_login_at
        """,
        profile["provider"],
        profile["providerUserId"],
        profile.get("email"),
        profile.get("displayName"),
        profile.get("avatarUrl"),
        json.dumps(profile.get("profile") or {}, ensure_ascii=False),
    )


async def _current_user(request: Request):
    if not db.pool:
        return None
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    row = await db.fetchrow(
        """
        select u.id, u.provider, u.provider_user_id, u.email, u.display_name, u.avatar_url,
          u.created_at, u.last_login_at
        from user_sessions s
        join app_users u on u.id = s.user_id
        where s.token_hash = $1
          and s.revoked_at is null
          and s.expires_at > now()
        """,
        _hash_token(token),
    )
    return row


async def _require_user(request: Request):
    user = await _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def _user_payload(user) -> dict:
    if not user:
        return {}
    return {
        "id": str(user["id"]),
        "provider": user["provider"],
        "email": user["email"],
        "displayName": user["display_name"],
        "avatarUrl": user["avatar_url"],
        "createdAt": user["created_at"].isoformat() if user["created_at"] else None,
        "lastLoginAt": user["last_login_at"].isoformat() if user["last_login_at"] else None,
    }


def _valid_pnu(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{19}", text) else None


@app.get("/")
async def index():
    return _html_response()


@app.get("/signin")
async def signin():
    return _html_response()


@app.get("/signup")
async def signup():
    return _html_response()


def _html_response():
    html = Path(__file__).resolve().parents[2] / "gruplan.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="gruplan.html을 찾을 수 없습니다.")
    return FileResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/gruplan_logo.png")
async def logo():
    logo_path = Path(__file__).resolve().parents[2] / "gruplan_logo.png"
    if not logo_path.exists():
        raise HTTPException(status_code=404, detail="gruplan_logo.png not found")
    return FileResponse(logo_path, media_type="image/png", headers={"Cache-Control": "no-store"})


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
        "auth": {"providers": _auth_providers_configured()},
        "sources": len(PUBLIC_DATA_SOURCES),
    }


@app.get("/api/data-sources")
async def data_sources():
    loaded_tables = await _loaded_tables()
    table_counts = await _table_row_estimates(loaded_tables)
    configured_keys = {
        "data": bool(settings.data_go_kr_service_key),
        "vworld": bool(settings.vworld_api_key),
    }
    items = []
    for source in PUBLIC_DATA_SOURCES:
        row_count = table_counts.get(source.table_name or "", None)
        status = "연결 가능"
        if source.table_name:
            if source.table_name not in loaded_tables:
                status = "스키마 확인 필요"
            elif not row_count:
                status = "실시간 공간 조회 연결" if source.id in FGIS_LIVE_SOURCE_IDS else "원천 데이터 적재 필요"
            else:
                status = f"공간 DB 적재 완료 · {row_count:,}건"
        if source.requires_key:
            key_ready = configured_keys["vworld"] if source.id == "D12" else configured_keys["data"]
            status = "API 키 확인 필요" if not key_ready else status
        if source.id in CSV_FILE_SOURCE_IDS:
            status = "공식 CSV 원본 연결"
        elif source.id in LIVE_OPEN_API_SOURCE_IDS and configured_keys["data"]:
            status = "OpenAPI 연결 가능"
        elif source.id in SNAPSHOT_SOURCE_IDS:
            snapshot = _forest_company_snapshot()
            if snapshot:
                total = int(snapshot.get("totalCount") or 0)
                status = f"공식 스냅샷 연결 · {total:,}건"
            elif configured_keys["data"]:
                status = "OpenAPI 연결 가능"
        item = source.to_dict()
        item["status"] = status
        if row_count is not None:
            item["rowCount"] = row_count
        items.append(item)
    return {"items": items}


@app.get("/api/client-config")
async def client_config():
    return {
        "vworldKey": settings.vworld_api_key,
        "vworldDomain": settings.vworld_referer,
    }


@app.get("/api/me")
async def current_user(request: Request):
    user = await _current_user(request)
    return {
        "user": _user_payload(user) if user else None,
        "auth": {"providers": _auth_providers_configured()},
    }


@app.post("/auth/email/signup")
async def email_signup(payload: EmailAuthPayload):
    if not db.pool:
        raise HTTPException(status_code=503, detail="DATABASE_URL이 필요합니다.")
    email = _normalize_email(payload.email)
    existing = await db.fetchrow(
        "select id from app_users where provider = 'email' and provider_user_id = $1",
        email,
    )
    if existing:
        raise HTTPException(status_code=409, detail="이미 가입된 이메일입니다. 로그인으로 이어가주세요.")
    display_name = (payload.display_name or email.split("@", 1)[0]).strip()[:80]
    row = await db.fetchrow(
        """
        insert into app_users (provider, provider_user_id, email, display_name, password_hash, email_verified_at)
        values ('email', $1, $1, $2, $3, now())
        returning id, provider, provider_user_id, email, display_name, avatar_url, created_at, last_login_at
        """,
        email,
        display_name,
        _hash_password(payload.password),
    )
    return await _email_auth_response(row, payload.next)


@app.post("/auth/email/login")
async def email_login(payload: EmailAuthPayload):
    if not db.pool:
        raise HTTPException(status_code=503, detail="DATABASE_URL이 필요합니다.")
    email = _normalize_email(payload.email)
    row = await db.fetchrow(
        """
        select id, provider, provider_user_id, email, display_name, avatar_url,
          password_hash, created_at, last_login_at
        from app_users
        where provider = 'email' and provider_user_id = $1
        """,
        email,
    )
    if not row or not _verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호를 확인해주세요.")
    user = await db.fetchrow(
        """
        update app_users set last_login_at = now()
        where id = $1
        returning id, provider, provider_user_id, email, display_name, avatar_url, created_at, last_login_at
        """,
        row["id"],
    )
    return await _email_auth_response(user, payload.next)


@app.get("/auth/{provider}/start")
async def auth_start(provider: str, next: str | None = "/"):
    provider = provider.lower()
    next_path = _safe_next_path(next)
    state = _signed_state({"provider": provider, "next": next_path, "iat": int(time.time()), "nonce": secrets.token_urlsafe(10)})
    redirect_uri = _auth_redirect_uri(provider)
    if provider == "google":
        if not settings.google_client_id or not settings.google_client_secret:
            raise HTTPException(status_code=503, detail="Google OAuth 환경변수가 필요합니다.")
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")
    if provider == "kakao":
        if not settings.kakao_client_id:
            raise HTTPException(status_code=503, detail="Kakao OAuth 환경변수가 필요합니다.")
        params = {
            "client_id": settings.kakao_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return RedirectResponse(f"https://kauth.kakao.com/oauth/authorize?{urlencode(params)}")
    raise HTTPException(status_code=404, detail="지원하지 않는 로그인 방식입니다.")


@app.get("/auth/{provider}/callback")
async def auth_callback(provider: str, code: str | None = None, state: str | None = None, error: str | None = None):
    provider = provider.lower()
    if error:
        return RedirectResponse(f"/signin?{urlencode({'error': error})}")
    if not code or not state:
        return RedirectResponse("/signin?error=missing_code")
    state_payload = _verify_state(state, provider)
    if not state_payload:
        return RedirectResponse("/signin?error=invalid_state")
    try:
        if provider == "google":
            profile = await _google_profile(code)
        elif provider == "kakao":
            profile = await _kakao_profile(code)
        else:
            raise HTTPException(status_code=404, detail="지원하지 않는 로그인 방식입니다.")
        user = await _upsert_oauth_user(profile)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("oauth callback failed: %s", exc, exc_info=True)
        return RedirectResponse("/signin?error=oauth_callback")
    raw_token = await _create_session(user)
    response = RedirectResponse(_safe_next_path(state_payload.get("next") or "/"))
    _set_session_cookie(response, raw_token)
    return response


@app.post("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token and db.pool:
        await db.execute(
            "update user_sessions set revoked_at = now() where token_hash = $1",
            _hash_token(token),
        )
    response = {"ok": True}
    return_response = Response(json.dumps(response), media_type="application/json")
    return_response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return return_response


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
    if row is None and _valid_pnu(payload.pnu) and not payload.geometry:
        enriched_payload = await _payload_with_vworld_geometry(payload)
        if enriched_payload.geometry:
            row = await _query_spatial_features(enriched_payload)
    if row is None:
        raise HTTPException(status_code=404, detail="해당 필지를 찾지 못했습니다.")

    raw_features = _json_object(row["features"])
    client_features = _json_object(payload.client_features)
    if client_features:
        await _cache_client_features(client_features)
        raw_features = _merge_client_features(raw_features, client_features)
    fgis_features = {}
    if payload.include_live:
        fgis_features = await _live_fgis_features(row["center_lon"], row["center_lat"])
        if fgis_features:
            await _cache_fgis_features(fgis_features)
            raw_features = _merge_fgis_features(raw_features, fgis_features)
    stand_properties = _feature_properties(raw_features, "stand")
    soil_properties = _feature_properties(raw_features, "soil")
    stand_age_class = _extract_int_property(stand_properties, STAND_AGE_KEYS, row["stand_age_class"])
    stand_species = _extract_text_property(stand_properties, STAND_SPECIES_KEYS)
    slope_degree = _extract_slope_degree(soil_properties, row["slope_degree"])
    planting_fit_count = max(int(row["planting_fit_count"] or 0), _fgis_planting_count(fgis_features))
    economic_forest = bool(row["economic_forest"]) or _fgis_economic_forest(fgis_features)
    avg_landslide_grade = _number_or_none(row["avg_landslide_grade"])
    if avg_landslide_grade is None:
        avg_landslide_grade = _fgis_landslide_grade(fgis_features)
    fire_risk_index = None
    if payload.include_live and row["pnu"]:
        live_fire_risk = await _live_fire_risk_for_pnu(row["pnu"])
        if live_fire_risk:
            raw_features["fireRisk"] = live_fire_risk
            fire_risk_index = _extract_fire_risk_index(live_fire_risk)
    if fire_risk_index is None:
        fire_risk_index = _extract_vworld_fire_risk_index(_feature_properties(raw_features, "fireRiskSpatial"))
    raw_features["derived"] = {
        "standAgeClass": stand_age_class,
        "standSpecies": stand_species,
        "slopeDegree": slope_degree,
        "fireRiskIndex": fire_risk_index,
        "plantingFitCount": planting_fit_count,
        "economicForest": economic_forest,
        "roadDistanceM": _number_or_none(row["road_distance_m"]),
        "roadDensityMPerHa": _number_or_none(row["road_density_m_per_ha"]),
        "avgLandslideGrade": avg_landslide_grade,
    }
    features = FeatureSet(
        area_ha=float(row["area_ha"]) if row["area_ha"] is not None else None,
        road_distance_m=float(row["road_distance_m"]) if row["road_distance_m"] is not None else None,
        road_density_m_per_ha=float(row["road_density_m_per_ha"]) if row["road_density_m_per_ha"] is not None else None,
        slope_degree=slope_degree,
        avg_landslide_grade=avg_landslide_grade,
        high_landslide_ratio=_number_or_none(row["high_landslide_ratio"]),
        fire_risk_index=fire_risk_index,
        economic_forest=economic_forest,
        planting_fit_count=planting_fit_count,
        stand_age_class=stand_age_class,
        stand_species=stand_species,
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


@app.get("/api/carbon-offset-projects")
async def carbon_offset_projects(page: int = 1, perPage: int = 20):
    try:
        return await public_client.carbon_offset_projects(page=page, per_page=perPage)
    except PublicDataError as exc:
        return _public_error(exc)


@app.get("/api/resource-stats")
async def resource_stats(classId: str | None = None):
    try:
        return await public_client.resource_stats(class_id=classId)
    except PublicDataError as exc:
        return _public_error(exc)


@app.post("/api/work-requests")
async def create_work_request(payload: WorkRequestPayload, request: Request):
    if not db.pool:
        raise HTTPException(status_code=503, detail="DATABASE_URL이 필요합니다.")
    user = await _current_user(request)
    analysis = payload.analysis or {}
    parcel = analysis.get("parcel") or {}
    scores = analysis.get("scores") or {}
    tasks = analysis.get("workPlan") or []
    work_type = payload.work_type or _quote_work_type(scores)
    user_parcel = await _upsert_user_parcel(user, analysis=analysis) if user else None
    row = await db.fetchrow(
        """
        insert into work_requests (
          pnu, address, admin_name, area_ha, work_type, recommended_scenario,
          risk_score, access_score, expected_tasks, quote, analysis, status, user_id, user_parcel_id
        )
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11::jsonb,$12,$13,$14)
        returning id, pnu, address, admin_name, area_ha, work_type, recommended_scenario,
          risk_score, access_score, status, created_at
        """,
        parcel.get("pnu"),
        parcel.get("address"),
        parcel.get("adminName"),
        _number_or_none(parcel.get("areaHa")),
        work_type,
        scores.get("recommendedScenario"),
        _number_or_none(scores.get("disasterRisk")),
        _number_or_none(scores.get("accessibility")),
        json.dumps(tasks, ensure_ascii=False),
        json.dumps(_quote_payload(analysis, work_type), ensure_ascii=False),
        json.dumps(analysis, ensure_ascii=False),
        payload.status,
        user["id"] if user else None,
        user_parcel["id"] if user_parcel else None,
    )
    return {"item": _work_request_row(row)}


@app.get("/api/work-requests/summary")
async def work_request_summary(region: str | None = None):
    if not db.pool:
        raise HTTPException(status_code=503, detail="DATABASE_URL이 필요합니다.")
    region_text = (region or "").strip() or None
    work_rows = await db.fetch(
        """
        select id, pnu, address, admin_name, area_ha, work_type, recommended_scenario,
          risk_score, access_score, status, created_at
        from work_requests
        where $1::text is null
          or admin_name ilike '%' || $1 || '%'
          or address ilike '%' || $1 || '%'
        order by created_at desc
        limit 200
        """,
        region_text,
    )
    work_items = [_work_request_row(row) for row in work_rows]
    for item in work_items:
        item["sourceType"] = "견적 수요"
    warnings = []
    try:
        candidates = await _regional_parcel_candidates(region_text)
    except Exception as exc:
        logger.warning("regional parcel candidate aggregation failed: %s", exc, exc_info=True)
        candidates = []
        warnings.append(
            {
                "scope": "publicCandidates",
                "message": "공간 후보 계산을 완료하지 못했습니다.",
            }
        )
    combined = _dashboard_items(work_items, candidates)
    regional_data = await _regional_public_data(region_text, work_items, candidates)
    return {
        "region": region_text,
        "overview": _dashboard_overview(work_items, candidates, combined, regional_data),
        "byWorkType": _dashboard_work_type_rows(combined),
        "riskBuckets": _dashboard_risk_buckets(combined),
        "recent": combined[:20],
        "workRequests": work_items[:20],
        "publicCandidates": candidates[:20],
        "regionalData": regional_data,
        "emptyState": _dashboard_empty_state(region_text) if not combined else None,
        "warnings": warnings,
    }


@app.get("/api/me/dashboard")
async def my_dashboard(request: Request):
    user = await _require_user(request)
    parcels = [_user_parcel_row(row) for row in await _user_parcel_rows(user)]
    analyses = [_analysis_record_row(row) for row in await _analysis_record_rows(user)]
    tasks = [_task_row(row) for row in await _task_rows(user)]
    notes = [_field_note_row(row) for row in await _field_note_rows(user)]
    documents = [_document_row(row) for row in await _document_rows(user)]
    alerts = [_alert_row(row) for row in await _alert_rows(user)]
    shares = [_share_row(row) for row in await _share_rows(user)]
    quotes = [_work_request_row(row) for row in await _user_quote_rows(user)]
    open_tasks = [task for task in tasks if task.get("status") not in {"완료", "보류"}]
    return {
        "user": _user_payload(user),
        "summary": {
            "parcelCount": len(parcels),
            "analysisCount": len(analyses),
            "openTaskCount": len(open_tasks),
            "noteCount": len(notes),
            "documentCount": len(documents),
            "alertCount": len([alert for alert in alerts if alert.get("status") != "완료"]),
            "quoteCount": len(quotes),
        },
        "parcels": parcels,
        "analyses": analyses,
        "tasks": tasks,
        "fieldNotes": notes,
        "documents": documents,
        "alerts": alerts,
        "shares": shares,
        "workRequests": quotes,
    }


@app.post("/api/me/parcels")
async def save_my_parcel(payload: UserParcelPayload, request: Request):
    user = await _require_user(request)
    row = await _upsert_user_parcel(user, analysis=payload.analysis, parcel=payload.parcel, note=payload.note)
    analysis = payload.analysis or {}
    if analysis:
        await _insert_analysis_record(user, analysis, row["id"], _analysis_record_title(analysis))
        await _ensure_analysis_alerts(user, row["id"], analysis)
    return {"item": _user_parcel_row(row)}


@app.get("/api/me/parcels")
async def my_parcels(request: Request):
    user = await _require_user(request)
    return {"items": [_user_parcel_row(row) for row in await _user_parcel_rows(user)]}


@app.post("/api/me/analyses")
async def save_analysis_record(payload: AnalysisRecordPayload, request: Request):
    user = await _require_user(request)
    parcel = await _upsert_user_parcel(user, analysis=payload.analysis)
    row = await _insert_analysis_record(user, payload.analysis, parcel["id"] if parcel else None, payload.title)
    if parcel:
        await _ensure_analysis_alerts(user, parcel["id"], payload.analysis)
    return {"item": _analysis_record_row(row)}


@app.get("/api/me/analyses")
async def my_analyses(request: Request):
    user = await _require_user(request)
    return {"items": [_analysis_record_row(row) for row in await _analysis_record_rows(user)]}


@app.post("/api/me/tasks")
async def create_task(payload: WorkTaskPayload, request: Request):
    user = await _require_user(request)
    row = await db.fetchrow(
        """
        insert into work_tasks (user_id, user_parcel_id, pnu, title, category, status, due_date, note)
        values ($1,$2,$3,$4,$5,$6,$7::date,$8)
        returning id, user_parcel_id, pnu, title, category, status, due_date, note, created_at, updated_at
        """,
        user["id"],
        payload.user_parcel_id,
        _valid_pnu(payload.pnu),
        payload.title.strip(),
        payload.category.strip() or "현장 확인",
        payload.status.strip() or "대기",
        payload.due_date,
        payload.note,
    )
    return {"item": _task_row(row)}


@app.patch("/api/me/tasks/{task_id}")
async def update_task(task_id: str, payload: WorkTaskUpdatePayload, request: Request):
    user = await _require_user(request)
    row = await db.fetchrow(
        """
        update work_tasks set
          title = coalesce($3, title),
          category = coalesce($4, category),
          status = coalesce($5, status),
          due_date = coalesce($6::date, due_date),
          note = coalesce($7, note),
          updated_at = now()
        where id = $1::uuid and user_id = $2
        returning id, user_parcel_id, pnu, title, category, status, due_date, note, created_at, updated_at
        """,
        task_id,
        user["id"],
        payload.title,
        payload.category,
        payload.status,
        payload.due_date,
        payload.note,
    )
    if not row:
        raise HTTPException(status_code=404, detail="업무를 찾지 못했습니다.")
    return {"item": _task_row(row)}


@app.get("/api/me/tasks")
async def my_tasks(request: Request):
    user = await _require_user(request)
    return {"items": [_task_row(row) for row in await _task_rows(user)]}


@app.post("/api/me/field-notes")
async def create_field_note(payload: FieldNotePayload, request: Request):
    user = await _require_user(request)
    row = await db.fetchrow(
        """
        insert into field_notes (user_id, user_parcel_id, pnu, note, lat, lon, attachments)
        values ($1,$2,$3,$4,$5,$6,$7::jsonb)
        returning id, user_parcel_id, pnu, note, lat, lon, attachments, created_at
        """,
        user["id"],
        payload.user_parcel_id,
        _valid_pnu(payload.pnu),
        payload.note.strip(),
        payload.lat,
        payload.lon,
        json.dumps(payload.attachments, ensure_ascii=False),
    )
    return {"item": _field_note_row(row)}


@app.get("/api/me/field-notes")
async def my_field_notes(request: Request):
    user = await _require_user(request)
    return {"items": [_field_note_row(row) for row in await _field_note_rows(user)]}


@app.post("/api/me/documents")
async def create_document(payload: UserDocumentPayload, request: Request):
    user = await _require_user(request)
    row = await db.fetchrow(
        """
        insert into user_documents (user_id, user_parcel_id, pnu, name, kind, source, payload)
        values ($1,$2,$3,$4,$5,$6,$7::jsonb)
        returning id, user_parcel_id, pnu, name, kind, source, payload, created_at
        """,
        user["id"],
        payload.user_parcel_id,
        _valid_pnu(payload.pnu),
        payload.name.strip(),
        payload.kind.strip() or "분석 문서",
        payload.source,
        json.dumps(payload.payload, ensure_ascii=False),
    )
    return {"item": _document_row(row)}


@app.get("/api/me/documents")
async def my_documents(request: Request):
    user = await _require_user(request)
    return {"items": [_document_row(row) for row in await _document_rows(user)]}


@app.post("/api/me/shares")
async def create_share(payload: SharePayload, request: Request):
    user = await _require_user(request)
    user_parcel_id = payload.user_parcel_id
    if not user_parcel_id and payload.pnu:
        row = await db.fetchrow(
            "select id from user_parcels where user_id = $1 and pnu = $2",
            user["id"],
            _valid_pnu(payload.pnu),
        )
        user_parcel_id = str(row["id"]) if row else None
    if not user_parcel_id:
        raise HTTPException(status_code=400, detail="공유할 필지를 먼저 저장해주세요.")
    token = secrets.token_urlsafe(24)
    row = await db.fetchrow(
        """
        insert into user_shares (user_id, user_parcel_id, share_token, permission, expires_at)
        values ($1,$2::uuid,$3,$4,$5::timestamptz)
        returning id, user_parcel_id, share_token, permission, created_at, expires_at
        """,
        user["id"],
        user_parcel_id,
        token,
        payload.permission,
        payload.expires_at,
    )
    return {"item": _share_row(row)}


@app.get("/api/share/{token}")
async def read_share(token: str):
    row = await db.fetchrow(
        """
        select s.share_token, s.permission, s.created_at, s.expires_at,
          p.pnu, p.address, p.admin_name, p.area_ha, p.parcel, p.last_analysis
        from user_shares s
        join user_parcels p on p.id = s.user_parcel_id
        where s.share_token = $1
          and (s.expires_at is null or s.expires_at > now())
        """,
        token,
    )
    if not row:
        raise HTTPException(status_code=404, detail="공유 링크를 찾지 못했습니다.")
    return {"item": _shared_parcel_row(row)}


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


async def _table_row_estimates(table_names: set[str]) -> dict[str, int]:
    if not db.pool or not table_names:
        return {}
    rows = await db.fetch(
        """
        select c.relname as table_name,
               greatest(coalesce(s.n_live_tup, c.reltuples, 0)::bigint, 0) as row_count
        from pg_class c
        left join pg_stat_all_tables s on s.relid = c.oid
        where c.relkind in ('r', 'p')
          and c.relname = any($1::text[])
        """,
        list(table_names),
    )
    counts = {row["table_name"]: int(row["row_count"] or 0) for row in rows}
    for table_name in sorted(table_names):
        if counts.get(table_name, 0) > 0 or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            continue
        row = await db.fetchrow(f'select count(*) as row_count from "{table_name}"')
        counts[table_name] = int(row["row_count"] or 0) if row else 0
    return counts


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
      select ST_Distance(a.geom::geography, r.geom::geography) as road_distance_m
      from area_calc a
      left join lateral (
        select geom
        from forest_roads
        order by geom <-> a.geom
        limit 1
      ) r on true
    ),
    road_density as (
      select coalesce(sum(ST_Length(ST_Intersection(a.geom, r.geom)::geography)), 0) / nullif(max(a.area_ha), 0) as road_density_m_per_ha
      from area_calc a
      left join forest_roads r on ST_Intersects(a.geom, r.geom)
    ),
    soil as (
      select
        s.properties,
        case
          when s.geom is null then null
          when ST_Intersects(a.geom, s.geom) then '필지 교차'
          else '근접 보정'
        end as match_type,
        case when s.geom is null then null else ST_Distance(a.geom::geography, s.geom::geography) end as distance_m,
        case when s.geom is null or not ST_Intersects(a.geom, s.geom) then 0
             else ST_Area(ST_Intersection(a.geom, s.geom)::geography)
        end as overlap_area_m2
      from area_calc a
      left join lateral (
        select properties, geom
        from forest_soils s
        where ST_Intersects(a.geom, s.geom)
           or ST_DWithin(a.geom::geography, s.geom::geography, 1000)
        order by
          case when ST_Intersects(a.geom, s.geom) then 0 else 1 end,
          case when ST_Intersects(a.geom, s.geom) then ST_Area(ST_Intersection(a.geom, s.geom)::geography) else 0 end desc,
          s.geom <-> a.geom
        limit 1
      ) s on true
    ),
    stand as (
      select
        s.properties,
        case
          when s.geom is null then null
          when ST_Intersects(a.geom, s.geom) then '필지 교차'
          else '인근 보정'
        end as match_type,
        case when s.geom is null then null else ST_Distance(a.geom::geography, s.geom::geography) end as distance_m,
        case when s.geom is null or not ST_Intersects(a.geom, s.geom) then 0
             else ST_Area(ST_Intersection(a.geom, s.geom)::geography)
        end as overlap_area_m2
      from area_calc a
      left join lateral (
        select properties, geom
        from forest_stands s
        where ST_Intersects(a.geom, s.geom)
           or ST_DWithin(a.geom::geography, s.geom::geography, 1000)
        order by
          case when ST_Intersects(a.geom, s.geom) then 0 else 1 end,
          case when ST_Intersects(a.geom, s.geom) then ST_Area(ST_Intersection(a.geom, s.geom)::geography) else 0 end desc,
          s.geom <-> a.geom
        limit 1
      ) s on true
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
      ST_X(ST_PointOnSurface(a.geom)) as center_lon,
      ST_Y(ST_PointOnSurface(a.geom)) as center_lat,
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
        'soilMatch', jsonb_build_object(
          'matchType', soil.match_type,
          'distanceM', soil.distance_m,
          'overlapAreaM2', soil.overlap_area_m2
        ),
        'stand', stand.properties,
        'standMatch', jsonb_build_object(
          'matchType', stand.match_type,
          'distanceM', stand.distance_m,
          'overlapAreaM2', stand.overlap_area_m2
        ),
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
        geometry = json.dumps(payload.geometry)
    return await db.fetchrow(sql, _valid_pnu(payload.pnu), geometry)


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _feature_properties(features: dict, key: str) -> dict:
    value = features.get(key) if isinstance(features, dict) else None
    return value if isinstance(value, dict) else {}


def _merge_client_features(raw_features: dict, client_features: dict) -> dict:
    merged = dict(raw_features or {})
    mapping = {
        "soil": "soil",
        "fireRiskSpatial": "fireRiskSpatial",
        "disasterZone": "disasterZone",
        "forestPromotionZone": "forestPromotionZone",
        "forestProtectionZone": "forestProtectionZone",
        "cadastral": "cadastral",
    }
    for client_key, target_key in mapping.items():
        feature = client_features.get(client_key)
        properties = _client_feature_properties(feature)
        if properties and not _feature_properties(merged, target_key):
            merged[target_key] = properties
    if _client_feature_properties(client_features.get("soil")) and not merged.get("soilMatch"):
        merged["soilMatch"] = {"matchType": "VWorld 2D 데이터", "distanceM": 0, "overlapAreaM2": None}
    return merged


def _client_feature_properties(feature) -> dict:
    if not isinstance(feature, dict):
        return {}
    properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else None
    if properties:
        return properties
    return feature if any(key not in {"type", "geometry", "id"} for key in feature.keys()) else {}


async def _live_fgis_features(lon, lat) -> dict:
    if lon is None or lat is None:
        return {}
    try:
        x, y = _lonlat_to_epsg5179(float(lon), float(lat))
    except (TypeError, ValueError):
        return {}
    async with httpx.AsyncClient(verify=False, timeout=12.0) as client:
        tasks = [
            _fetch_fgis_feature(client, key, dataset, x, y)
            for key, (dataset, _) in FGIS_LAYER_CONFIG.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    features = {}
    for result in results:
        if isinstance(result, Exception) or not result:
            continue
        key, feature = result
        if feature:
            features[key] = feature
    return features


async def _fetch_fgis_feature(client: httpx.AsyncClient, key: str, dataset: str, x: float, y: float):
    body = {
        "getFeatureMode": "SPATIAL",
        "datasetNames": [f"FDMS_BASE:{dataset}"],
        "spatialQueryMode": "INTERSECT",
        "geometry": {"type": "POINT", "points": [{"x": x, "y": y}], "parts": [1]},
        "fromIndex": 0,
        "toIndex": 0,
        "returnContent": True,
    }
    response = await client.post(
        FGIS_ENDPOINT,
        json=body,
        headers={"Referer": "https://map.forest.go.kr/forest", "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    data = response.json()
    feature = _first_supermap_feature(data, dataset)
    return key, feature


def _first_supermap_feature(data: dict, dataset: str) -> dict | None:
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list) or not features:
        return None
    feature = features[0]
    properties = dict(zip(feature.get("fieldNames") or [], feature.get("fieldValues") or []))
    if not properties:
        return None
    feature_id = str(feature.get("ID") or properties.get("SMID") or "").strip()
    properties["_sourceDataset"] = dataset
    properties["_sourceName"] = "산림공간정보서비스"
    properties["_sourceFeatureId"] = feature_id
    geometry = _supermap_geometry_to_geojson(feature.get("geometry"))
    result = {"type": "Feature", "id": f"fgis:{dataset}:{feature_id}", "properties": properties}
    if geometry:
        result["geometry"] = geometry
        result["geometryCrs"] = "EPSG:5179"
    return result


def _supermap_geometry_to_geojson(geometry: dict | None) -> dict | None:
    if not isinstance(geometry, dict):
        return None
    geom_type = str(geometry.get("type") or "").upper()
    points = geometry.get("points")
    parts = geometry.get("parts") or [len(points or [])]
    if not isinstance(points, list) or not points:
        return None

    def coords_for(part_points):
        coords = []
        for point in part_points:
            if not isinstance(point, dict):
                continue
            x = _number_or_none(point.get("x"))
            y = _number_or_none(point.get("y"))
            if x is not None and y is not None:
                coords.append([x, y])
        return coords

    offset = 0
    if geom_type in {"REGION", "POLYGON"}:
        polygons = []
        for part in parts:
            count = int(_number_or_none(part) or 0)
            ring = coords_for(points[offset : offset + count])
            offset += count
            if len(ring) < 3:
                continue
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            polygons.append([ring])
        if not polygons:
            return None
        return {"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1 else {"type": "MultiPolygon", "coordinates": polygons}
    if geom_type in {"LINE", "LINESTRING"}:
        lines = []
        for part in parts:
            count = int(_number_or_none(part) or 0)
            line = coords_for(points[offset : offset + count])
            offset += count
            if len(line) >= 2:
                lines.append(line)
        if not lines:
            return None
        return {"type": "LineString", "coordinates": lines[0]} if len(lines) == 1 else {"type": "MultiLineString", "coordinates": lines}
    if geom_type == "POINT":
        coords = coords_for(points[:1])
        return {"type": "Point", "coordinates": coords[0]} if coords else None
    return None


def _lonlat_to_epsg5179(lon: float, lat: float) -> tuple[float, float]:
    semi_major = 6378137.0
    flattening = 1 / 298.257222101
    eccentricity_sq = 2 * flattening - flattening * flattening
    second_eccentricity_sq = eccentricity_sq / (1 - eccentricity_sq)
    phi = math.radians(lat)
    lam = math.radians(lon)
    phi0 = math.radians(38.0)
    lam0 = math.radians(127.5)
    scale = 0.9996
    false_easting = 1_000_000.0
    false_northing = 2_000_000.0

    def meridian_arc(value: float) -> float:
        e2 = eccentricity_sq
        return semi_major * (
            (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * value
            - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * value)
            + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * value)
            - (35 * e2**3 / 3072) * math.sin(6 * value)
        )

    radius = semi_major / math.sqrt(1 - eccentricity_sq * math.sin(phi) ** 2)
    tangent_sq = math.tan(phi) ** 2
    eta_sq = second_eccentricity_sq * math.cos(phi) ** 2
    a_value = math.cos(phi) * (lam - lam0)
    x = false_easting + scale * radius * (
        a_value
        + (1 - tangent_sq + eta_sq) * a_value**3 / 6
        + (5 - 18 * tangent_sq + tangent_sq**2 + 72 * eta_sq - 58 * second_eccentricity_sq) * a_value**5 / 120
    )
    y = false_northing + scale * (
        meridian_arc(phi)
        - meridian_arc(phi0)
        + radius
        * math.tan(phi)
        * (
            a_value**2 / 2
            + (5 - tangent_sq + 9 * eta_sq + 4 * eta_sq**2) * a_value**4 / 24
            + (61 - 58 * tangent_sq + tangent_sq**2 + 600 * eta_sq - 330 * second_eccentricity_sq)
            * a_value**6
            / 720
        )
    )
    return x, y


def _fgis_feature_properties(feature) -> dict:
    if not isinstance(feature, dict):
        return {}
    return feature.get("properties") if isinstance(feature.get("properties"), dict) else {}


def _merge_fgis_features(raw_features: dict, fgis_features: dict) -> dict:
    merged = dict(raw_features or {})
    stand = _fgis_feature_properties(fgis_features.get("stand"))
    if stand:
        merged["stand"] = {**_feature_properties(merged, "stand"), **stand}
        merged["standMatch"] = {"matchType": "산림공간정보서비스 교차", "distanceM": 0, "overlapAreaM2": None}
    soil = _fgis_feature_properties(fgis_features.get("soilFgis"))
    if soil:
        merged["soil"] = {**_feature_properties(merged, "soil"), **soil}
        merged["soilFgis"] = soil
        merged["soilMatch"] = {"matchType": "산림공간정보서비스 교차", "distanceM": 0, "overlapAreaM2": None}
    for key in ("planting5000", "planting25000", "landslideVector", "economicNational", "economicPrivate"):
        properties = _fgis_feature_properties(fgis_features.get(key))
        if properties:
            merged[key] = properties
    return merged


def _fgis_planting_count(fgis_features: dict) -> int:
    return sum(1 for key in ("planting5000", "planting25000") if _fgis_feature_properties(fgis_features.get(key)))


def _fgis_economic_forest(fgis_features: dict) -> bool:
    return any(_fgis_feature_properties(fgis_features.get(key)) for key in ("economicNational", "economicPrivate"))


def _fgis_landslide_grade(fgis_features: dict) -> float | None:
    properties = _fgis_feature_properties(fgis_features.get("landslideVector"))
    return _number_or_none(properties.get("GRIDCODE")) if properties else None


async def _cache_client_features(client_features: dict) -> None:
    if not db.pool:
        return
    cadastral = client_features.get("cadastral")
    soil = client_features.get("soil")
    if isinstance(cadastral, dict):
        await _cache_cadastral_feature(cadastral)
    if isinstance(soil, dict):
        await _cache_vector_feature("forest_soils", soil)


async def _cache_fgis_features(fgis_features: dict) -> None:
    if not db.pool or not isinstance(fgis_features, dict):
        return
    for key, (_, table_name) in FGIS_LAYER_CONFIG.items():
        if not table_name:
            continue
        feature = fgis_features.get(key)
        if isinstance(feature, dict):
            await _cache_fgis_vector_feature(table_name, feature)


async def _cache_cadastral_feature(feature: dict) -> None:
    properties = _client_feature_properties(feature)
    geometry = feature.get("geometry")
    pnu = _valid_pnu(properties.get("pnu") or properties.get("PNU"))
    if not pnu or not isinstance(geometry, dict):
        return
    await db.execute(
        """
        insert into parcels (pnu, address, admin_name, properties, geom)
        values (
          $1,
          $2,
          null,
          $3::jsonb,
          ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON($4), 4326))
        )
        on conflict (pnu) do update set
          address = coalesce(excluded.address, parcels.address),
          properties = excluded.properties,
          geom = excluded.geom
        """,
        pnu,
        properties.get("addr") or properties.get("address") or properties.get("jibun"),
        json.dumps(properties, ensure_ascii=False),
        json.dumps(geometry),
    )


async def _cache_vector_feature(table_name: str, feature: dict) -> None:
    if table_name not in {"forest_soils"}:
        return
    properties = _client_feature_properties(feature)
    geometry = feature.get("geometry")
    source_feature_id = str(feature.get("id") or properties.get("id") or "").strip()
    if not source_feature_id or not isinstance(geometry, dict):
        return
    await db.execute(
        f"""
        insert into {table_name} (source_feature_id, properties, geom)
        select
          $1,
          $2::jsonb,
          ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON($3), 4326))
        where not exists (
          select 1 from {table_name} where source_feature_id = $1
        )
        """,
        source_feature_id,
        json.dumps(properties, ensure_ascii=False),
        json.dumps(geometry),
    )


async def _cache_fgis_vector_feature(table_name: str, feature: dict) -> None:
    if table_name not in {"forest_stands", "forest_soils", "planting_zones", "economic_forest_zones"}:
        return
    properties = _fgis_feature_properties(feature)
    geometry = feature.get("geometry")
    source_feature_id = str(feature.get("id") or properties.get("_sourceFeatureId") or "").strip()
    if not source_feature_id or not isinstance(geometry, dict):
        return
    await db.execute(
        f"""
        insert into {table_name} (source_feature_id, properties, geom)
        select
          $1,
          $2::jsonb,
          ST_Multi(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON($3), 5179), 4326))
        where not exists (
          select 1 from {table_name} where source_feature_id = $1
        )
        """,
        source_feature_id,
        json.dumps(properties, ensure_ascii=False),
        json.dumps(geometry),
    )


def _normalize_property_key(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum() or "\uac00" <= ch <= "\ud7a3")


def _find_property(properties: dict, keys: list[str]):
    if not isinstance(properties, dict):
        return None, None
    normalized = {_normalize_property_key(key): (key, value) for key, value in properties.items()}
    for key in keys:
        found = normalized.get(_normalize_property_key(key))
        if found and found[1] not in (None, ""):
            return found
    wanted = [_normalize_property_key(key) for key in keys]
    for actual_key, value in properties.items():
        actual = _normalize_property_key(actual_key)
        if value in (None, ""):
            continue
        if any(token and (token in actual or actual in token) for token in wanted):
            return actual_key, value
    return None, None


def _extract_int_property(properties: dict, keys: list[str], fallback=None) -> int | None:
    _, value = _find_property(properties, keys)
    if value in (None, ""):
        value = fallback
    number = _number_or_none(value)
    if number is None:
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        number = _number_or_none(digits) if digits else None
    return int(number) if number is not None else None


def _extract_text_property(properties: dict, keys: list[str]) -> str | None:
    key, value = _find_property(properties, keys)
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized_key = _normalize_property_key(key or "")
    if normalized_key in {"koftrgroupcd", "koftrgroup", "frtpcd", "frtp"}:
        return {"1": "침엽수림", "2": "활엽수림", "3": "혼효림"}.get(text, f"수종 코드 {text}")
    if normalized_key.endswith("cd") and text.isdigit():
        return f"수종 코드 {text}"
    return text


def _extract_slope_degree(properties: dict, fallback=None) -> float | None:
    key, value = _find_property(properties, SLOPE_KEYS)
    if value in (None, ""):
        value = fallback
    number = _number_or_none(value)
    if number is None:
        return None
    normalized_key = _normalize_property_key(key or "")
    if number <= 6 and any(token in normalized_key for token in ["cd", "code", "typ", "type", "등급", "급"]):
        return {1: 3, 2: 10, 3: 17.5, 4: 22.5, 5: 27.5, 6: 35}.get(int(number), number)
    return number


async def _live_fire_risk_for_pnu(pnu: str) -> dict | None:
    sigungu_code = _current_admin_code(str(pnu)[:5], 5)
    if not sigungu_code:
        return None
    try:
        return await public_client.fire_risk(sigunguCode=sigungu_code)
    except PublicDataError:
        return None


def _extract_fire_risk_index(data: dict) -> float | None:
    item = data.get("response", {}).get("body", {}).get("items", {}).get("item") if isinstance(data, dict) else None
    if isinstance(item, list) and item:
        item = item[0]
    if not isinstance(item, dict):
        return None
    return _number_or_none(item.get("meanavg")) or _number_or_none(item.get("maxi"))


def _extract_vworld_fire_risk_index(properties: dict) -> float | None:
    if not isinstance(properties, dict):
        return None
    values = []
    for key, value in properties.items():
        key_text = str(key).lower()
        if not key_text.startswith("value") or not key_text.endswith("h"):
            continue
        number = _number_or_none(value)
        if number is not None and number > 0:
            values.append(number)
    return max(values) if values else None


def _number_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        return float(match.group(0)) if match else None


def _round_or_none(value, digits: int = 1):
    number = _number_or_none(value)
    return round(number, digits) if number is not None else None


def _quote_work_type(scores: dict) -> str:
    scenario = str(scores.get("recommendedScenario") or "")
    if "수익" in scenario:
        return "숲가꾸기·생산 기반 견적"
    if "탄소" in scenario:
        return "탄소상쇄 후보 조사"
    if "보전" in scenario:
        return "보전·복원 진단"
    if "재난" in scenario:
        return "재난저감 사전 점검"
    return "현장 기본조사"


def _quote_payload(analysis: dict, work_type: str) -> dict:
    parcel = analysis.get("parcel") or {}
    scores = analysis.get("scores") or {}
    features = analysis.get("features") or {}
    derived = features.get("derived") if isinstance(features.get("derived"), dict) else {}
    return {
        "workType": work_type,
        "parcel": parcel,
        "scores": {
            "risk": scores.get("disasterRisk"),
            "access": scores.get("accessibility"),
            "recommendedScenario": scores.get("recommendedScenario"),
        },
        "site": {
            "roadDistanceM": derived.get("roadDistanceM"),
            "roadDensityMPerHa": derived.get("roadDensityMPerHa"),
            "slopeDegree": derived.get("slopeDegree"),
            "standAgeClass": derived.get("standAgeClass"),
            "standSpecies": derived.get("standSpecies"),
            "avgLandslideGrade": derived.get("avgLandslideGrade"),
            "fireRiskIndex": derived.get("fireRiskIndex"),
        },
        "expectedTasks": analysis.get("workPlan") or [],
    }


def _work_request_row(row) -> dict:
    if row is None:
        return {}
    created_at = row["created_at"]
    return {
        "id": str(row["id"]),
        "pnu": row["pnu"],
        "address": row["address"],
        "adminName": row["admin_name"],
        "areaHa": _round_or_none(row["area_ha"], 2),
        "workType": row["work_type"],
        "recommendedScenario": row["recommended_scenario"],
        "riskScore": _round_or_none(row["risk_score"]),
        "accessScore": _round_or_none(row["access_score"]),
        "status": row["status"],
        "createdAt": created_at.isoformat() if created_at else None,
    }


async def _upsert_user_parcel(user, analysis: dict | None = None, parcel: dict | None = None, note: str | None = None):
    if not user:
        return None
    analysis = analysis or {}
    parcel = parcel or analysis.get("parcel") or {}
    pnu = _valid_pnu(parcel.get("pnu"))
    if not pnu:
        raise HTTPException(status_code=400, detail="저장할 PNU가 필요합니다.")
    row = await db.fetchrow(
        """
        insert into user_parcels (user_id, pnu, address, admin_name, area_ha, parcel, last_analysis, note)
        values ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8)
        on conflict (user_id, pnu) do update set
          address = coalesce(excluded.address, user_parcels.address),
          admin_name = coalesce(excluded.admin_name, user_parcels.admin_name),
          area_ha = coalesce(excluded.area_ha, user_parcels.area_ha),
          parcel = excluded.parcel,
          last_analysis = case when excluded.last_analysis = '{}'::jsonb then user_parcels.last_analysis else excluded.last_analysis end,
          note = coalesce(excluded.note, user_parcels.note),
          updated_at = now()
        returning id, pnu, address, admin_name, area_ha, parcel, last_analysis, note, created_at, updated_at
        """,
        user["id"],
        pnu,
        parcel.get("address"),
        parcel.get("adminName"),
        _number_or_none(parcel.get("areaHa")),
        json.dumps(parcel, ensure_ascii=False),
        json.dumps(analysis, ensure_ascii=False),
        note,
    )
    return row


async def _insert_analysis_record(user, analysis: dict, user_parcel_id, title: str | None = None):
    parcel = analysis.get("parcel") or {}
    row = await db.fetchrow(
        """
        insert into analysis_records (user_id, user_parcel_id, pnu, title, analysis)
        values ($1,$2,$3,$4,$5::jsonb)
        returning id, user_parcel_id, pnu, title, analysis, created_at
        """,
        user["id"],
        user_parcel_id,
        _valid_pnu(parcel.get("pnu")),
        title or _analysis_record_title(analysis),
        json.dumps(analysis, ensure_ascii=False),
    )
    return row


def _analysis_record_title(analysis: dict) -> str:
    parcel = analysis.get("parcel") or {}
    address = parcel.get("address") or parcel.get("pnu") or "선택 필지"
    scenario = (analysis.get("scores") or {}).get("recommendedScenario") or "경영 분석"
    return f"{address} · {scenario}"


async def _ensure_analysis_alerts(user, user_parcel_id, analysis: dict) -> None:
    scores = analysis.get("scores") or {}
    parcel = analysis.get("parcel") or {}
    pnu = _valid_pnu(parcel.get("pnu"))
    risk = _number_or_none(scores.get("disasterRisk"))
    scenario = str(scores.get("recommendedScenario") or "")
    alerts = []
    if risk is not None and risk >= 60:
        alerts.append(("재난위험 점검", f"재난위험 점수 {round(risk)}점입니다. 우기 전 배수와 사면 하단을 확인하세요.", "주의"))
    if "탄소" in scenario:
        alerts.append(("탄소상쇄 확인", "영급, 수종, 제외 면적을 현장 기록에 채운 뒤 탄소상쇄 사례와 비교하세요.", "안내"))
    if not alerts:
        alerts.append(("현장 기록 보완", "임도 접근, 경사, 수종, 영급을 현장에서 확인하면 다음 분석 품질이 올라갑니다.", "안내"))
    for title, message, level in alerts:
        await db.execute(
            """
            insert into user_alerts (user_id, user_parcel_id, pnu, title, message, level)
            select $1,$2,$3,$4,$5,$6
            where not exists (
              select 1 from user_alerts
              where user_id = $1 and user_parcel_id = $2 and title = $4 and status <> '완료'
            )
            """,
            user["id"],
            user_parcel_id,
            pnu,
            title,
            message,
            level,
        )


async def _user_parcel_rows(user):
    return await db.fetch(
        """
        select id, pnu, address, admin_name, area_ha, parcel, last_analysis, note, created_at, updated_at
        from user_parcels
        where user_id = $1
        order by updated_at desc
        limit 100
        """,
        user["id"],
    )


async def _analysis_record_rows(user):
    return await db.fetch(
        """
        select id, user_parcel_id, pnu, title, analysis, created_at
        from analysis_records
        where user_id = $1
        order by created_at desc
        limit 80
        """,
        user["id"],
    )


async def _task_rows(user):
    return await db.fetch(
        """
        select id, user_parcel_id, pnu, title, category, status, due_date, note, created_at, updated_at
        from work_tasks
        where user_id = $1
        order by
          case status when '진행 중' then 0 when '대기' then 1 when '보류' then 2 else 3 end,
          due_date nulls last,
          updated_at desc
        limit 120
        """,
        user["id"],
    )


async def _field_note_rows(user):
    return await db.fetch(
        """
        select id, user_parcel_id, pnu, note, lat, lon, attachments, created_at
        from field_notes
        where user_id = $1
        order by created_at desc
        limit 80
        """,
        user["id"],
    )


async def _document_rows(user):
    return await db.fetch(
        """
        select id, user_parcel_id, pnu, name, kind, source, payload, created_at
        from user_documents
        where user_id = $1
        order by created_at desc
        limit 80
        """,
        user["id"],
    )


async def _alert_rows(user):
    return await db.fetch(
        """
        select id, user_parcel_id, pnu, title, message, level, due_at, status, created_at
        from user_alerts
        where user_id = $1
        order by
          case status when '대기' then 0 when '확인' then 1 else 2 end,
          due_at nulls last,
          created_at desc
        limit 80
        """,
        user["id"],
    )


async def _share_rows(user):
    return await db.fetch(
        """
        select id, user_parcel_id, share_token, permission, created_at, expires_at
        from user_shares
        where user_id = $1
        order by created_at desc
        limit 50
        """,
        user["id"],
    )


async def _user_quote_rows(user):
    return await db.fetch(
        """
        select id, pnu, address, admin_name, area_ha, work_type, recommended_scenario,
          risk_score, access_score, status, created_at
        from work_requests
        where user_id = $1
        order by created_at desc
        limit 80
        """,
        user["id"],
    )


def _user_parcel_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "pnu": row["pnu"],
        "address": row["address"],
        "adminName": row["admin_name"],
        "areaHa": _round_or_none(row["area_ha"], 2),
        "parcel": _json_object(row["parcel"]),
        "lastAnalysis": _json_object(row["last_analysis"]),
        "note": row["note"],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _analysis_record_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "userParcelId": str(row["user_parcel_id"]) if row["user_parcel_id"] else None,
        "pnu": row["pnu"],
        "title": row["title"],
        "analysis": _json_object(row["analysis"]),
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _task_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "userParcelId": str(row["user_parcel_id"]) if row["user_parcel_id"] else None,
        "pnu": row["pnu"],
        "title": row["title"],
        "category": row["category"],
        "status": row["status"],
        "dueDate": row["due_date"].isoformat() if row["due_date"] else None,
        "note": row["note"],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _field_note_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "userParcelId": str(row["user_parcel_id"]) if row["user_parcel_id"] else None,
        "pnu": row["pnu"],
        "note": row["note"],
        "lat": _round_or_none(row["lat"], 6),
        "lon": _round_or_none(row["lon"], 6),
        "attachments": row["attachments"] if isinstance(row["attachments"], list) else [],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _document_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "userParcelId": str(row["user_parcel_id"]) if row["user_parcel_id"] else None,
        "pnu": row["pnu"],
        "name": row["name"],
        "kind": row["kind"],
        "source": row["source"],
        "payload": _json_object(row["payload"]),
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _alert_row(row) -> dict:
    return {
        "id": str(row["id"]),
        "userParcelId": str(row["user_parcel_id"]) if row["user_parcel_id"] else None,
        "pnu": row["pnu"],
        "title": row["title"],
        "message": row["message"],
        "level": row["level"],
        "dueAt": row["due_at"].isoformat() if row["due_at"] else None,
        "status": row["status"],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _share_row(row) -> dict:
    token = row["share_token"]
    return {
        "id": str(row["id"]),
        "userParcelId": str(row["user_parcel_id"]) if row["user_parcel_id"] else None,
        "shareToken": token,
        "url": f"{settings.app_base_url.rstrip('/')}/api/share/{token}",
        "permission": row["permission"],
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "expiresAt": row["expires_at"].isoformat() if row["expires_at"] else None,
    }


def _shared_parcel_row(row) -> dict:
    return {
        "shareToken": row["share_token"],
        "permission": row["permission"],
        "pnu": row["pnu"],
        "address": row["address"],
        "adminName": row["admin_name"],
        "areaHa": _round_or_none(row["area_ha"], 2),
        "parcel": _json_object(row["parcel"]),
        "lastAnalysis": _json_object(row["last_analysis"]),
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        "expiresAt": row["expires_at"].isoformat() if row["expires_at"] else None,
    }


async def _regional_public_data(region_text: str | None, work_items: list[dict], candidates: list[dict]) -> dict:
    region = (region_text or "").strip()
    context = _regional_context(region, work_items, candidates)
    if not region and not context.get("sigunguCode"):
        return {"context": context, "sources": []}

    calls: dict[str, tuple[str, object]] = {}
    sigungu_code = context.get("sigunguCode")
    if sigungu_code:
        calls["fireRisk"] = ("D6", public_client.fire_risk(sigunguCode=sigungu_code))
    if region:
        calls["companies"] = ("D10", public_client.forest_companies(region=region))
        calls["economicForest"] = ("D8", public_client.economic_forest(search=region))
    calls["carbon"] = ("D9", public_client.carbon_offset_projects(page=1, per_page=1000))
    calls["resourceStats"] = ("D11", public_client.resource_stats())

    results = {}
    if calls:
        gathered = await asyncio.gather(
            *[_safe_public_call(source_id, call) for source_id, call in calls.values()],
            return_exceptions=False,
        )
        results = dict(zip(calls.keys(), gathered))

    data = {
        "context": context,
        "fireRisk": _summarize_fire_risk(results.get("fireRisk")),
        "companies": _summarize_companies(results.get("companies")),
        "economicForest": _summarize_economic_forest(results.get("economicForest")),
        "carbon": _summarize_carbon_projects(results.get("carbon"), region),
        "resourceStats": _summarize_resource_stats(results.get("resourceStats")),
    }
    data["sources"] = _regional_source_rows(data)
    return data


async def _safe_public_call(source_id: str, call) -> dict:
    try:
        return {"ok": True, "sourceId": source_id, "data": await call}
    except PublicDataError as exc:
        return {"ok": False, "sourceId": exc.source_id or source_id, "message": str(exc)}
    except Exception as exc:
        logger.warning("regional public data call failed: %s", source_id, exc_info=True)
        return {"ok": False, "sourceId": source_id, "message": "데이터 조회를 완료하지 못했습니다."}


def _regional_context(region_text: str, work_items: list[dict], candidates: list[dict]) -> dict:
    items = [*work_items, *candidates]
    sigungu_code = ""
    for item in items:
        pnu = str(item.get("pnu") or "")
        if re.fullmatch(r"\d{19}", pnu):
            sigungu_code = _current_admin_code(pnu[:5], 5)
            break
    if not sigungu_code:
        sigungu_code = _region_code_hint(region_text)
    return {
        "region": region_text or None,
        "sigunguCode": sigungu_code or None,
        "sidoCode": sigungu_code[:2] if sigungu_code else None,
    }


def _region_code_hint(region_text: str | None) -> str:
    text = re.sub(r"\s+", "", str(region_text or ""))
    if not text:
        return ""
    for name, code in REGION_CODE_HINTS.items():
        if name in text or text in name:
            return code
    return ""


def _public_result_data(result: dict | None) -> dict:
    if isinstance(result, dict) and result.get("ok") and isinstance(result.get("data"), dict):
        return result["data"]
    return {}


def _public_result_error(result: dict | None) -> str | None:
    if isinstance(result, dict) and not result.get("ok"):
        return str(result.get("message") or "조회 실패")
    return None


def _public_items(data: dict | None) -> list[dict]:
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("data"), list):
        return [item for item in data["data"] if isinstance(item, dict)]
    body = data.get("response", {}).get("body", {}) if isinstance(data.get("response"), dict) else {}
    items = body.get("items", {}) if isinstance(body, dict) else {}
    item = items.get("item") if isinstance(items, dict) else items
    if isinstance(item, list):
        return [row for row in item if isinstance(row, dict)]
    if isinstance(item, dict):
        return [item]
    if isinstance(items, list):
        return [row for row in items if isinstance(row, dict)]
    return []


def _public_total_count(data: dict | None, items: list[dict]) -> int:
    if not isinstance(data, dict):
        return len(items)
    for value in (
        data.get("totalCount"),
        data.get("response", {}).get("body", {}).get("totalCount") if isinstance(data.get("response"), dict) else None,
    ):
        number = _number_or_none(value)
        if number is not None:
            return int(number)
    return len(items)


def _summarize_fire_risk(result: dict | None) -> dict:
    error = _public_result_error(result)
    data = _public_result_data(result)
    items = _public_items(data)
    item = items[0] if items else {}
    index = _number_or_none(item.get("meanavg")) or _number_or_none(item.get("maxi"))
    return {
        "sourceId": "D6",
        "status": "연결" if item else ("대기" if not error else "확인"),
        "index": _round_or_none(index),
        "max": _round_or_none(item.get("maxi")),
        "today": _round_or_none(item.get("d3")),
        "tomorrow": _round_or_none(item.get("d4")),
        "regionName": item.get("sigun") or item.get("signguNm") or item.get("doname"),
        "level": _risk_level(index),
        "message": error,
    }


def _risk_level(value) -> str:
    number = _number_or_none(value)
    if number is None:
        return "확인 중"
    if number >= 80:
        return "매우 높음"
    if number >= 60:
        return "높음"
    if number >= 40:
        return "관리 필요"
    return "낮음"


def _summarize_companies(result: dict | None) -> dict:
    error = _public_result_error(result)
    data = _public_result_data(result)
    items = _public_items(data)
    active = [item for item in items if _active_company(item)]
    target_items = active or items
    spec_counts: dict[str, int] = {}
    for item in target_items:
        spec = _clean_text(item.get("specnm") or item.get("specNm") or "업종 미기재")
        spec_counts[spec] = spec_counts.get(spec, 0) + 1
    top_specs = [
        {"name": name, "count": count}
        for name, count in sorted(spec_counts.items(), key=lambda row: (-row[1], row[0]))[:5]
    ]
    return {
        "sourceId": "D10",
        "status": "연결" if items else ("대기" if not error else "확인"),
        "total": _public_total_count(data, items),
        "activeCount": len(active),
        "topSpecs": top_specs,
        "items": [_company_item(item) for item in target_items[:6]],
        "snapshotAt": (data.get("snapshot") or {}).get("fetchedAt") if isinstance(data.get("snapshot"), dict) else None,
        "message": error,
    }


def _active_company(item: dict) -> bool:
    useyn = _clean_text(item.get("useyn") or item.get("useYn"))
    cancel = _clean_text(item.get("canceldate") or item.get("cancelDate") or item.get("cancelcause"))
    if "취소" in useyn or "취소" in cancel:
        return False
    if "최종" in useyn:
        return True
    return not cancel


def _company_item(item: dict) -> dict:
    return {
        "name": _clean_text(item.get("tradename") or item.get("tradeName") or item.get("companyName")),
        "address": _clean_text(item.get("address")),
        "field": _clean_text(item.get("specnm") or item.get("specNm")),
        "technics": _clean_text(item.get("technics")),
        "status": _clean_text(item.get("useyn") or item.get("useYn")),
    }


def _summarize_economic_forest(result: dict | None) -> dict:
    error = _public_result_error(result)
    data = _public_result_data(result)
    items = _public_items(data)
    return {
        "sourceId": "D8",
        "status": "연결" if items else ("대기" if not error else "확인"),
        "total": _public_total_count(data, items),
        "items": [_compact_public_item(item, 3) for item in items[:5]],
        "message": error,
    }


def _summarize_carbon_projects(result: dict | None, region_text: str | None) -> dict:
    error = _public_result_error(result)
    data = _public_result_data(result)
    items = _public_items(data)
    keywords = _region_keywords(region_text)
    matched = []
    if keywords:
        for item in items:
            text = " ".join(_clean_text(value) for value in item.values())
            if any(keyword and keyword in text for keyword in keywords):
                matched.append(item)
    target_items = matched or items[:6]
    area_total = sum(_number_or_none(_find_public_value(item, ["면적"])) or 0 for item in matched)
    absorption_total = sum(_number_or_none(_find_public_value(item, ["총", "흡수"])) or 0 for item in matched)
    return {
        "sourceId": "D9",
        "status": "연결" if items else ("대기" if not error else "확인"),
        "total": _public_total_count(data, items),
        "matchedCount": len(matched),
        "matchedAreaHa": round(area_total, 2),
        "matchedAbsorption": round(absorption_total, 1),
        "items": [_carbon_item(item) for item in target_items[:6]],
        "message": error,
    }


def _region_keywords(region_text: str | None) -> list[str]:
    text = re.sub(r"\s+", "", str(region_text or ""))
    if not text:
        return []
    keywords = {text}
    match = re.search(r"([가-힣]+(?:시|군|구))", text)
    if match:
        keywords.add(match.group(1))
    for suffix in ("특별자치도", "광역시", "특별시", "자치도", "도", "시", "군", "구"):
        if text.endswith(suffix) and len(text) > len(suffix):
            keywords.add(text[: -len(suffix)])
    return sorted(keywords, key=len, reverse=True)


def _carbon_item(item: dict) -> dict:
    return {
        "projectNo": _clean_text(_find_public_value(item, ["등록", "번호"]) or _find_public_value(item, ["고유", "번호"])),
        "type": _clean_text(_find_public_value(item, ["종류"]) or _find_public_value(item, ["유형"])),
        "operator": _clean_text(_find_public_value(item, ["사업자"])),
        "areaHa": _round_or_none(_find_public_value(item, ["면적"]), 2),
        "absorption": _round_or_none(_find_public_value(item, ["총", "흡수"])),
        "content": _clean_text(_find_public_value(item, ["내용"])),
    }


def _summarize_resource_stats(result: dict | None) -> dict:
    error = _public_result_error(result)
    data = _public_result_data(result)
    items = _public_items(data)
    return {
        "sourceId": "D11",
        "status": "연결" if items else ("대기" if not error else "확인"),
        "total": _public_total_count(data, items),
        "items": [_resource_stat_item(item) for item in items[:6]],
        "message": error,
    }


def _resource_stat_item(item: dict) -> dict:
    return {
        "name": _clean_text(item.get("statNm") or item.get("statName") or item.get("statClsscNm") or item.get("clsscNm")),
        "classId": _clean_text(item.get("statClsscId") or item.get("clsscId")),
    }


def _regional_source_rows(data: dict) -> list[dict]:
    rows = []
    labels = {
        "fireRisk": "산불위험예보",
        "companies": "산림사업법인",
        "economicForest": "경제림육성단지",
        "carbon": "탄소상쇄 등록",
        "resourceStats": "산림자원통계",
    }
    for key, label in labels.items():
        value = data.get(key) or {}
        rows.append(
            {
                "id": value.get("sourceId"),
                "name": label,
                "status": value.get("status") or "대기",
                "count": value.get("total") or value.get("index") or value.get("activeCount"),
                "message": value.get("message"),
            }
        )
    return rows


def _compact_public_item(item: dict, limit: int = 4) -> dict:
    compact = {}
    for key, value in item.items():
        if len(compact) >= limit:
            break
        text = _clean_text(value)
        if text:
            compact[_clean_text(key)] = text
    return compact


def _find_public_value(item: dict, tokens: list[str]):
    if not isinstance(item, dict):
        return None
    for key, value in item.items():
        key_text = _clean_text(key)
        if all(token in key_text for token in tokens) and value not in (None, ""):
            return value
    return None


def _clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


async def _regional_parcel_candidates(region_text: str | None, limit: int = 40) -> list[dict]:
    if not db.pool:
        return []
    rows = await db.fetch(
        """
        with raw_base as (
          select pnu, address, admin_name, geom
          from parcels
          where pnu is not null
            and geom is not null
            and ($1::text is null or admin_name ilike '%' || $1 || '%' or address ilike '%' || $1 || '%')
          order by pnu
          limit $2
        ),
        base as (
          select
            pnu,
            address,
            admin_name,
            clean_geom as geom,
            ST_Area(clean_geom::geography) / 10000.0 as area_ha
          from (
            select
              pnu,
              address,
              admin_name,
              ST_CollectionExtract(ST_MakeValid(geom), 3) as clean_geom
            from raw_base
          ) cleaned
          where not ST_IsEmpty(clean_geom)
        )
        select
          b.pnu,
          b.address,
          b.admin_name,
          b.area_ha,
          coalesce((
            select count(*)::int
            from planting_zones z
            where z.geom && b.geom
              and ST_Intersects(b.geom, ST_CollectionExtract(ST_MakeValid(z.geom), 3))
          ), 0) as planting_fit_count,
          exists (
            select 1
            from economic_forest_zones e
            where e.geom && b.geom
              and ST_Intersects(b.geom, ST_CollectionExtract(ST_MakeValid(e.geom), 3))
          ) as economic_forest,
          (
            select s.properties
            from forest_stands s
            where s.geom && b.geom
              and ST_Intersects(b.geom, ST_CollectionExtract(ST_MakeValid(s.geom), 3))
            order by ST_Area(ST_Intersection(b.geom, ST_CollectionExtract(ST_MakeValid(s.geom), 3))::geography) desc
            limit 1
          ) as stand_properties,
          (
            select s.properties
            from forest_soils s
            where s.geom && b.geom
              and ST_Intersects(b.geom, ST_CollectionExtract(ST_MakeValid(s.geom), 3))
            order by ST_Area(ST_Intersection(b.geom, ST_CollectionExtract(ST_MakeValid(s.geom), 3))::geography) desc
            limit 1
          ) as soil_properties
        from base b
        """,
        region_text,
        limit,
    )
    candidates = [_candidate_from_summary_row(row) for row in rows]
    candidates = [candidate for candidate in candidates if candidate]
    candidates.sort(key=lambda item: (-(item.get("riskScore") or 0), item.get("accessScore") if item.get("accessScore") is not None else 100))
    return candidates


def _candidate_from_summary_row(row) -> dict | None:
    if row is None:
        return None
    stand_properties = _json_object(row["stand_properties"])
    soil_properties = _json_object(row["soil_properties"])
    stand_age_class = _extract_int_property(stand_properties, STAND_AGE_KEYS)
    slope_degree = _extract_slope_degree(soil_properties, None)
    features = FeatureSet(
        area_ha=float(row["area_ha"]) if row["area_ha"] is not None else None,
        slope_degree=slope_degree,
        economic_forest=bool(row["economic_forest"]),
        planting_fit_count=int(row["planting_fit_count"] or 0),
        stand_age_class=stand_age_class,
        stand_species=_extract_text_property(stand_properties, STAND_SPECIES_KEYS),
    )
    scores = score_features(features)
    work_type = _quote_work_type(scores)
    return {
        "id": f"candidate-{row['pnu'] or row['address'] or 'parcel'}",
        "pnu": row["pnu"],
        "address": row["address"],
        "adminName": row["admin_name"],
        "areaHa": _round_or_none(features.area_ha, 2),
        "workType": work_type,
        "recommendedScenario": scores.get("recommendedScenario"),
        "riskScore": _round_or_none(scores.get("disasterRisk")),
        "accessScore": _round_or_none(scores.get("accessibility")),
        "status": "공공데이터 후보",
        "sourceType": "공공데이터 후보",
        "createdAt": None,
    }


def _dashboard_items(work_items: list[dict], candidates: list[dict]) -> list[dict]:
    seen = set()
    combined = []
    for item in work_items:
        key = item.get("pnu") or item.get("id")
        if key:
            seen.add(key)
        combined.append(item)
    for item in candidates:
        key = item.get("pnu") or item.get("id")
        if key and key in seen:
            continue
        combined.append(item)
    combined.sort(key=lambda item: (-(item.get("riskScore") or 0), item.get("accessScore") if item.get("accessScore") is not None else 100))
    return combined


def _dashboard_overview(
    work_items: list[dict],
    candidates: list[dict],
    combined: list[dict],
    regional_data: dict | None = None,
) -> dict:
    risk_values = [item["riskScore"] for item in combined if item.get("riskScore") is not None]
    access_values = [item["accessScore"] for item in combined if item.get("accessScore") is not None]
    regional_data = regional_data or {}
    fire_risk = regional_data.get("fireRisk") or {}
    companies = regional_data.get("companies") or {}
    carbon = regional_data.get("carbon") or {}
    economic_forest = regional_data.get("economicForest") or {}
    resource_stats = regional_data.get("resourceStats") or {}
    return {
        "total": len(combined),
        "demandCount": len(work_items),
        "candidateCount": len(candidates),
        "totalAreaHa": round(sum(item.get("areaHa") or 0 for item in combined), 2),
        "avgRisk": round(sum(risk_values) / len(risk_values), 1) if risk_values else None,
        "avgAccess": round(sum(access_values) / len(access_values), 1) if access_values else None,
        "fireRiskIndex": fire_risk.get("index"),
        "activeCompanyCount": companies.get("activeCount"),
        "companyCount": companies.get("total"),
        "carbonProjectCount": carbon.get("total"),
        "carbonMatchedCount": carbon.get("matchedCount"),
        "economicForestCount": economic_forest.get("total"),
        "resourceStatCount": resource_stats.get("total"),
    }


def _dashboard_empty_state(region_text: str | None) -> dict:
    region_label = region_text or "선택 지역"
    return {
        "title": f"{region_label} 집계 데이터가 아직 없습니다.",
        "message": "해당 지역 산지를 조회하거나 법인 탭에서 견적 수요를 저장하면 면적, 위험도, 접근성, 작업 유형 집계가 생성됩니다.",
        "actions": [
            "산주 탭에서 해당 시군구의 산지 필지를 선택",
            "공간분석 완료 후 법인 탭에서 견적 수요 저장",
            "저장된 수요와 공공 후보를 기준으로 우선 검토 목록 확인",
        ],
    }


def _dashboard_work_type_rows(items: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item.get("workType") or "현장 기본조사", []).append(item)
    rows = []
    for work_type, values in grouped.items():
        risks = [item["riskScore"] for item in values if item.get("riskScore") is not None]
        access = [item["accessScore"] for item in values if item.get("accessScore") is not None]
        rows.append(
            {
                "workType": work_type,
                "total": len(values),
                "areaHa": round(sum(item.get("areaHa") or 0 for item in values), 2),
                "avgRisk": round(sum(risks) / len(risks), 1) if risks else None,
                "avgAccess": round(sum(access) / len(access), 1) if access else None,
            }
        )
    return sorted(rows, key=lambda row: (-row["total"], row["workType"]))


def _dashboard_risk_buckets(items: list[dict]) -> list[dict]:
    buckets = {"고위험": 0, "관리 필요": 0, "일반": 0, "확인 필요": 0}
    for item in items:
        risk = item.get("riskScore")
        if risk is None:
            buckets["확인 필요"] += 1
        elif risk >= 70:
            buckets["고위험"] += 1
        elif risk >= 45:
            buckets["관리 필요"] += 1
        else:
            buckets["일반"] += 1
    return [{"bucket": key, "total": value} for key, value in buckets.items() if value]


def _first_vworld_point(search: dict) -> dict | None:
    items = search.get("response", {}).get("result", {}).get("items", [])
    if not items:
        return None
    point = items[0].get("point") or {}
    try:
        return {"lon": float(point["x"]), "lat": float(point["y"])}
    except (KeyError, TypeError, ValueError):
        return None


async def _payload_with_vworld_geometry(payload: AnalysisRequest) -> AnalysisRequest:
    pnu = _valid_pnu(payload.pnu)
    if not pnu:
        return payload
    try:
        cadastral = await public_client.cadastral_by_pnu(pnu)
    except PublicDataError:
        return payload
    feature = _first_vworld_feature(cadastral)
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    if not isinstance(geometry, dict):
        return payload
    return payload.model_copy(update={"geometry": geometry})


def _first_vworld_feature(data: dict) -> dict:
    collection = data.get("response", {}).get("result", {}).get("featureCollection", {})
    features = collection.get("features", []) if isinstance(collection, dict) else []
    if isinstance(features, list) and features:
        feature = features[0]
        return feature if isinstance(feature, dict) else {}
    return {}


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
            {"title": "순관리면적 도면 작성", "reason": "경계, 계곡부, 진입로 주변을 제외해 실제 관리 면적 확정", "season": "상반기"},
            {"title": "표본 조사구 배치", "reason": "영급과 임상 구성이 유지되는 구역을 기준선으로 기록", "season": "상반기"},
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
    age_text = str(features.stand_age_class) if features.stand_age_class is not None else "확인 필요"
    species = features.stand_species or "확인 필요"
    planting = features.planting_fit_count or 0
    economic = "경제림 구역에 포함" if features.economic_forest else "경제림 구역 미포함"
    road = _format_meters(features.road_distance_m)
    slope = _format_degree(features.slope_degree)
    landslide = _format_grade(features.avg_landslide_grade)
    density = _format_density(features.road_density_m_per_ha)
    access_ready = features.road_distance_m is not None and features.slope_degree is not None
    disaster_ready = features.avg_landslide_grade is not None or features.high_landslide_ratio is not None
    stand_ready = features.stand_age_class is not None
    access_gap = _missing_clause(
        [
            ("임도 거리", features.road_distance_m is not None),
            ("경사", features.slope_degree is not None),
        ]
    )
    disaster_gap = _missing_clause(
        [
            ("산사태 위험", disaster_ready),
            ("산불위험", features.fire_risk_index is not None),
        ]
    )
    resilience_gap = _missing_object_clause(
        [
            ("산사태 위험", disaster_ready),
            ("산불위험", features.fire_risk_index is not None),
            ("임도 거리", features.road_distance_m is not None),
            ("경사", features.slope_degree is not None),
        ]
    )
    profit_judgement = (
        f"이 필지는 {economic}, 조림 후보 {planting}건, 임도 거리 {road}, 경사 {slope}입니다. 접근성 {round(access)}점이 운반비와 작업 구역 면적을 제한하고, 임도 밀도 {density}는 내부 이동성 보정값으로 들어갔습니다."
        if access_ready
        else f"수익형 {round(scores.get('profit', 0) or 0)}점은 {economic}, 조림 후보 {planting}건, 현재 확인된 접근 조건을 합산한 값입니다. {access_gap} 보강되면 운반비와 실제 작업 가능 면적이 다시 계산됩니다."
    )
    if stand_ready and features.stand_species:
        carbon_judgement = (
            f"면적 {round(area, 2)}ha와 영급 {age}가 장기 관리 단위로 충분하고, 임상/수종 구성은 {species}입니다. "
            f"탄소형 점수에서 면적 기여는 {round(min(area * 1.6, 14), 1)}점, 영급 기여는 {round(min(age * 3.5, 22), 1)}점입니다."
        )
    elif stand_ready:
        carbon_judgement = (
            f"면적 {round(area, 2)}ha와 영급 {age}가 장기 흡수 관리 점수를 만들었습니다. "
            f"임상/수종 구성은 현장 표본조사에서 보정하고, 영급 기여는 {round(min(age * 3.5, 22), 1)}점입니다."
        )
    elif features.stand_species:
        carbon_judgement = (
            f"면적 {round(area, 2)}ha와 임상/수종 구성 {species}가 확인됐습니다. "
            f"영급 값이 들어오면 장기 흡수량과 등록 가능 면적을 더 좁혀 계산합니다."
        )
    else:
        carbon_judgement = (
            f"면적 {round(area, 2)}ha는 확인됐고, 임상도 속성에서 영급과 임상 구성을 찾는 중입니다. "
            f"탄소형 {round(scores.get('carbon', 0) or 0)}점은 현재 확보된 면적과 재난·접근 조건을 반영한 값입니다."
        )
    conservation_judgement = (
        f"산사태 평균등급은 {landslide}, 경사는 {slope}입니다. 보전형은 {round(scores.get('conservation', 0) or 0)}점이며, 전체를 보전지로 묶기보다 계곡부, 급경사, 위험 격자가 겹치는 부분만 별도 관리 대상으로 나누는 판단입니다."
        if disaster_ready and features.slope_degree is not None
        else f"보전형 {round(scores.get('conservation', 0) or 0)}점은 현재 확보된 지형과 위험 정보를 반영한 값입니다. {disaster_gap} 들어오면 계곡부와 급경사 경계를 더 좁혀 보전 구역을 분리합니다."
    )
    resilience_judgement = (
        f"재난위험 {round(disaster)}점, 접근성 {round(access)}점, 면적 {round(area, 2)}ha가 함께 들어갔습니다. 산사태 등급은 {landslide}, 진입 동선은 {road}이며 장마 전 배수와 임도 유실 점검을 앞순위로 둡니다."
        if disaster_ready and access_ready
        else f"재난저감형 {round(scores.get('resilience', 0) or 0)}점은 산사태·산불 지표와 접근 조건 중 확인된 값을 합산한 결과입니다. {resilience_gap} 보강한 뒤 물길, 임도, 사면 하단의 작업 순서를 확정합니다."
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
                f"영급: {age_text}",
                f"임상/수종 구성: {species}",
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
            "영급과 임상/수종 구성이 확인되기 전에는 탄소형 판단을 등록 가능성 확정보다 조사 후보로 봅니다.",
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
        *_surrounding_context(nearby_parcels, nearby_roads),
    ]
    if nearby_economic:
        common_context.append("주변 300m 안에 경제림 구역 근거가 있어 생산형 사업과의 충돌 여부를 함께 봅니다.")

    if top_name == "탄소형":
        species_text = features.stand_species or "임상 구성 확인 대상"
        thesis = f"이 필지는 면적 {round(area, 2)}ha, 영급 {age if age else '미확인'}, 임상/수종 구성 {species_text}가 탄소형 점수를 끌어올렸습니다. 접근성 {round(access)}점과 재난위험 {round(disaster)}점은 등록 가능성보다 유지관리 비용과 모니터링 동선 설계에서 더 크게 작용합니다."
        ideas = [
            "필지 전체를 한 덩어리로 보지 말고 경계, 계곡부, 접근로 주변을 제외한 순관리면적을 먼저 산정합니다.",
            _carbon_neighbor_idea(nearby_parcels),
            _carbon_access_idea(features, nearby_roads),
        ]
        phases = [
            {"name": "1단계 기준선 확정", "actions": ["경계와 제외면적 산정", "수종과 영급 표본조사", "기존 벌채나 피해 이력 확인"]},
            {"name": "2단계 흡수량 설계", "actions": ["관리 대상 면적별 흡수량 예비계산", "등록 사례와 면적, 수종, 영급 비교", "장기 모니터링 동선 배치"]},
            {"name": "3단계 실행 방식", "actions": ["보식, 간벌, 방치 구역을 나누어 관리", _phase_neighbor_action(nearby_parcels), "수익형 사업과 충돌하는 구역 제외"]},
        ]
        risks = [
            "영급이나 수종이 실제와 다르면 탄소형 점수는 바로 바뀝니다.",
            "접근성이 낮은 구간은 조사와 모니터링 비용이 커지므로 순관리면적을 줄이는 편이 현실적입니다.",
        ]
    elif top_name == "수익형":
        thesis = f"수익형이 앞선 이유는 조림 후보 {features.planting_fit_count or 0}건, 경제림 판정 {'포함' if features.economic_forest else '미포함'}, 접근성 {round(access)}점의 조합입니다. 재난위험 {round(disaster)}점이 낮을수록 작업 일정 선택 폭이 넓어집니다."
        ideas = [
            _profit_access_idea(features),
            "조림 후보 수종과 현재 임상을 비교해 모두 베는 방식보다 보식과 부분 갱신을 먼저 검토합니다.",
            _profit_neighbor_idea(nearby_parcels),
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
            _conservation_neighbor_idea(nearby_parcels),
        ]
        phases = [
            {"name": "1단계 민감구역 표시", "actions": ["고위험 사면과 계곡부 분리", "작업 제한선 설정", "토양 침식 흔적 확인"]},
            {"name": "2단계 저강도 관리", "actions": ["고사목과 병해충 구간만 선별", "배수 흐름 유지", "훼손 구간 복원 우선순위 작성"]},
            {"name": "3단계 활용 모델", "actions": ["보전형 지원사업 확인", _phase_neighbor_action(nearby_parcels), "장기 모니터링 지점 지정"]},
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
            _resilience_neighbor_idea(nearby_parcels),
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


def _missing_clause(items: list[tuple[str, bool]]) -> str:
    missing = [label for label, present in items if not present]
    if not missing:
        return "현장 보정값이"
    if len(missing) == 1:
        return f"{missing[0]} 항목이"
    if len(missing) == 2:
        return f"{missing[0]}와 {missing[1]} 항목이"
    return f"{', '.join(missing[:-1])}, {missing[-1]} 항목이"


def _missing_object_clause(items: list[tuple[str, bool]]) -> str:
    missing = [label for label, present in items if not present]
    if not missing:
        return "현장 보정값을"
    if len(missing) == 1:
        return f"{missing[0]} 항목을"
    if len(missing) == 2:
        return f"{missing[0]}와 {missing[1]} 항목을"
    return f"{', '.join(missing[:-1])}, {missing[-1]} 항목을"


def _surrounding_context(nearby_parcels: int, nearby_roads: int) -> list[str]:
    parcel_line = (
        f"주변 250m 안에 함께 검토할 필지 {nearby_parcels}개가 있어 공동 조사나 작업 동선 공유 가능성을 따져볼 수 있습니다."
        if nearby_parcels > 0
        else "주변 250m 안에 바로 묶을 필지 근거가 없어 공동관리보다 이 필지 단독의 순관리면적을 먼저 확정합니다."
    )
    road_line = (
        f"주변 500m 안에 임도 요소 {nearby_roads}개가 잡혀 현장에서는 연결 가능 지점과 실제 통행 상태를 확인합니다."
        if nearby_roads > 0
        else "주변 500m 안에 임도 요소가 잡히지 않아 지도상 길보다 실제 진입 가능 지점 확인이 먼저입니다."
    )
    return [parcel_line, road_line]


def _carbon_neighbor_idea(nearby_parcels: int) -> str:
    if nearby_parcels > 0:
        return "주변 필지와 조사 일정을 묶어 표본 조사와 장기 모니터링 비용을 나누는 공동 탄소관리안을 비교합니다."
    return "주변 공동관리 근거가 약하므로 소유 필지 안에서 기준선 조사, 제외면적 산정, 모니터링 동선을 완결하는 단독안을 우선 검토합니다."


def _carbon_access_idea(features: FeatureSet, nearby_roads: int) -> str:
    if features.road_distance_m is not None or nearby_roads > 0:
        return "임도와 연결되는 외곽부에 표본 조사구를 두고, 접근이 어려운 내부는 훼손을 줄이는 장기 관리 구역으로 분리합니다."
    return "임도 근접 근거가 약하므로 첫 현장 방문에서는 차량 진입점, 도보 접근선, 장기 모니터링 이동 시간을 먼저 지도에 표시합니다."


def _phase_neighbor_action(nearby_parcels: int) -> str:
    if nearby_parcels > 0:
        return "공동관리 대상 필지와 비용 분담 범위 확인"
    return "단독 관리 기준의 조사비와 모니터링 주기 확정"


def _profit_access_idea(features: FeatureSet) -> str:
    if features.road_distance_m is not None:
        return "가까운 임도에서 이어지는 구간부터 작업 블록을 나누고 운반비가 높은 내부 구역은 후순위로 둡니다."
    return "임도 근접값이 없으므로 생산성보다 실제 진입 가능 지점과 운반 거리 확인을 먼저 둡니다."


def _profit_neighbor_idea(nearby_parcels: int) -> str:
    if nearby_parcels > 0:
        return "주변 필지가 있으면 작업로와 운반 일정을 묶어 사업비를 낮추는 공동 발주를 검토합니다."
    return "주변 공동 발주 근거가 약하므로 작업 규모를 무리하게 키우기보다 가능한 구역만 잘라 견적을 받습니다."


def _conservation_neighbor_idea(nearby_parcels: int) -> str:
    if nearby_parcels > 0:
        return "주변 필지와 물길이 이어지는 경우 보전 협약이나 공동 배수 관리를 검토합니다."
    return "주변 연계보다 필지 내부의 계곡부, 급경사, 배수 불량 지점을 작은 보전 구역으로 분리합니다."


def _resilience_neighbor_idea(nearby_parcels: int) -> str:
    if nearby_parcels > 0:
        return "주변 필지와 물길이 이어지면 공동 저감 사업으로 배수와 진입 동선을 함께 정리합니다."
    return "주변 필지 연계보다 이 필지 안의 배수 흐름, 사면 하단, 진입 동선을 먼저 정리합니다."


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
            f"영급은 {age if age is not None else '원천 속성 확인 중'}, 임상/수종 구성은 {features.stand_species or '원천 속성 확인 중'}입니다.",
            f"현재 탄소형 {round(scores.get('carbon', 0) or 0)}점은 면적 {round(area, 2)}ha와 확인된 임상 속성만 반영한 값입니다.",
            "수관밀도와 경급까지 함께 잡히면 장기 관리, 보식, 부분 갱신 여부를 더 좁힐 수 있습니다.",
        ],
        "D2": [
            f"경사는 {slope}입니다. 값이 확인되면 접근성, 보전형, 재난저감형 판단이 함께 보정됩니다.",
            f"현재 접근성 {round(access)}점은 확인된 임도 정보와 경사 정보를 반영한 작업 동선 점수입니다.",
            "토심과 토성은 조림 후보 검토의 보정값으로 읽히며, 실제 장비 진입 전 현장 확인이 필요합니다.",
        ],
        "D3": [
            f"맞춤형 조림 후보가 {features.planting_fit_count or 0}건 교차되어 수익형 생산성 항목에 반영됐습니다.",
            f"후보 수가 많을수록 수종 선택지가 넓어지지만, 이 필지의 최종 추천은 {scores.get('recommendedScenario', '확인 필요')}입니다.",
            "후보 수종은 임상도에서 읽힌 현재 임상 구성과 겹치는지 확인해야 실행 계획으로 넘어갈 수 있습니다.",
        ],
        "D4": [
            f"가까운 임도 거리는 {road}, 필지 내부 임도 밀도는 {density}입니다.",
            f"임도 거리가 확인되면 수익형의 운반비 판단과 재난저감형의 긴급 진입 동선 판단이 함께 좁혀집니다.",
            "임도 상태가 실제 차량 통행에 맞지 않으면 수익형 점수는 현장 보정에서 낮아질 수 있습니다.",
        ],
        "D5": [
            f"산사태 평균등급은 {landslide}입니다. 값이 확인되지 않은 필지는 안전 판정이 아니라 위험 구간 산정 전 상태로 봅니다.",
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


def _format_index(value: float | None) -> str:
    if value is None:
        return "확인 필요"
    return str(round(value, 1)).rstrip("0").rstrip(".")


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
    access_gap = _missing_clause(
        [
            ("임도 거리", features.road_distance_m is not None),
            ("경사", features.slope_degree is not None),
        ]
    )
    disaster_gap = _missing_clause(
        [
            ("산사태 위험", features.avg_landslide_grade is not None or features.high_landslide_ratio is not None),
            ("산불위험", features.fire_risk_index is not None),
        ]
    )
    resilience_gap = _missing_clause(
        [
            ("산사태 위험", features.avg_landslide_grade is not None or features.high_landslide_ratio is not None),
            ("산불위험", features.fire_risk_index is not None),
            ("임도 거리", features.road_distance_m is not None),
        ]
    )
    access_interpretation = (
        f"임도 거리 {_format_meters(features.road_distance_m)}에서 {road_score}점, 경사 {_format_degree(features.slope_degree)}에서 감점 {slope_cost}점, 임도 밀도 보정 {road_bonus}점이 반영되어 접근성은 {round(scores.get('accessibility', 0) or 0)}점입니다."
        if features.road_distance_m is not None and features.slope_degree is not None
        else f"접근성은 현재 확인된 임도와 경사 정보를 조합해 {round(scores.get('accessibility', 0) or 0)}점으로 계산했습니다. {access_gap} 보강되면 작업로 판단과 수익형 점수가 다시 조정됩니다."
    )
    disaster_interpretation = (
        f"산사태 평균등급은 {_format_grade(features.avg_landslide_grade)}, 산불위험 지수는 {_format_index(features.fire_risk_index)}입니다. 두 위험 지표를 반영한 재난위험은 {round(scores.get('disasterRisk', 0) or 0)}점입니다."
        if features.avg_landslide_grade is not None or features.fire_risk_index is not None
        else "산사태와 산불위험 값이 연결되기 전에는 계곡부, 사면 하단, 임도 절개지를 현장 우선 확인 구역으로 둡니다."
    )
    if features.stand_age_class is not None and features.stand_species:
        carbon_interpretation = (
            f"면적 {round(features.area_ha or 0, 2)}ha가 {area_bonus}점, 영급 {features.stand_age_class}가 {carbon_age_bonus}점 기여했습니다. "
            f"임상/수종 구성 {features.stand_species}은 장기 관리 방식과 갱신 전략을 정하는 근거로 연결됩니다."
        )
    elif features.stand_age_class is not None:
        carbon_interpretation = (
            f"면적 {round(features.area_ha or 0, 2)}ha가 {area_bonus}점, 영급 {features.stand_age_class}가 {carbon_age_bonus}점 기여했습니다. "
            "임상/수종 구성은 임상도 속성표의 보조 항목으로 분리해 현장 조사값과 대조합니다."
        )
    elif features.stand_species:
        carbon_interpretation = (
            f"면적 {round(features.area_ha or 0, 2)}ha와 임상/수종 구성 {features.stand_species}가 반영됐습니다. "
            "영급 값이 연결되면 장기 흡수량과 탄소형 점수가 더 정밀해집니다."
        )
    else:
        carbon_interpretation = (
            f"면적 {round(features.area_ha or 0, 2)}ha는 반영됐고 임상도 속성에서 영급과 임상 구성을 찾는 중입니다. "
            f"탄소형 {round(scores.get('carbon', 0) or 0)}점은 현재 확보된 면적과 재난·접근 조건을 반영한 값입니다."
        )
    return {
        "method": "공공데이터 검색 체인과 가중치 기반 설명",
        "retrievalChain": [
            {"step": "필지 확정", "sourceIds": ["D12"], "evidence": "연속지적도 경계 또는 VWorld 조회 geometry를 분석 기준으로 사용"},
            {"step": "공간 교차", "sourceIds": ["D1", "D2", "D3", "D4", "D5", "D8"], "evidence": "필지와 산림공간정보를 교차하고, 교차값이 없을 때는 미교차 자체를 판단 근거로 기록"},
            {"step": "지표 산정", "sourceIds": ["D2", "D4", "D5", "D8"], "evidence": "접근성, 재난위험, 생산성, 보전성 지표 계산"},
            {"step": "경영 방향", "sourceIds": ["D1", "D3", "D9", "D10", "D11"], "evidence": "종합 지표와 실행 작업 후보 연결"},
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
                "interpretation": f"경제림 판정은 {'포함' if features.economic_forest else '미포함'}이고 조림 후보는 {features.planting_fit_count or 0}건입니다. 수익형 {round(scores.get('profit', 0) or 0)}점은 생산 조건과 작업 동선을 함께 본 값이며, {access_gap} 보강되면 사업비와 운반 동선 판단이 달라집니다.",
            },
            {
                "metric": "탄소형",
                "score": scores.get("carbon"),
                "formula": "기본점 + 영급 보정 + 면적 보정 + 위험·접근 보정",
                "sourceIds": ["D1", "D9"],
                "inputs": {
                    "areaHa": features.area_ha,
                    "standAgeClass": features.stand_age_class,
                    "standSpecies": features.stand_species,
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
                "interpretation": f"재난저감형 {round(scores.get('resilience', 0) or 0)}점은 면적 보정 {resilience_area_bonus}점과 접근 동선, 위험 지표를 함께 봅니다. {resilience_gap} 보강되면 배수와 임도 유실 점검 순서를 더 좁힙니다.",
            },
        ],
    }
