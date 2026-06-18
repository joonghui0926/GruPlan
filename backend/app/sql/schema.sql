create extension if not exists postgis;
create extension if not exists postgis_raster;
create extension if not exists pgcrypto;

create table if not exists public_data_sources (
  id text primary key,
  name text not null,
  provider text not null,
  kind text not null,
  access text not null,
  url text not null,
  usage text not null,
  table_name text,
  requires_key boolean not null default false,
  ingestion text not null,
  license_note text not null,
  last_checked_at timestamptz,
  last_http_status integer,
  last_error text
);

create table if not exists parcels (
  pnu text primary key,
  address text,
  admin_name text,
  properties jsonb not null default '{}'::jsonb,
  geom geometry(MultiPolygon, 4326) not null
);

create table if not exists forest_stands (
  id bigserial primary key,
  source_feature_id text,
  properties jsonb not null default '{}'::jsonb,
  geom geometry(MultiPolygon, 4326) not null
);

create table if not exists forest_soils (
  id bigserial primary key,
  source_feature_id text,
  properties jsonb not null default '{}'::jsonb,
  geom geometry(MultiPolygon, 4326) not null
);

create table if not exists planting_zones (
  id bigserial primary key,
  source_feature_id text,
  properties jsonb not null default '{}'::jsonb,
  geom geometry(MultiPolygon, 4326) not null
);

create table if not exists forest_roads (
  id bigserial primary key,
  source_feature_id text,
  properties jsonb not null default '{}'::jsonb,
  geom geometry(MultiLineString, 4326) not null
);

create table if not exists economic_forest_zones (
  id bigserial primary key,
  source_feature_id text,
  properties jsonb not null default '{}'::jsonb,
  geom geometry(MultiPolygon, 4326) not null
);

create table if not exists landslide_risk (
  rid serial primary key,
  rast raster not null,
  filename text,
  loaded_at timestamptz not null default now()
);

create table if not exists carbon_offset_projects (
  id bigserial primary key,
  project_no text,
  project_name text,
  project_type text,
  area_ha numeric,
  carbon_absorption numeric,
  properties jsonb not null default '{}'::jsonb,
  synced_at timestamptz not null default now()
);

create table if not exists forest_business_companies (
  id bigserial primary key,
  regno text,
  tradename text,
  captain text,
  address text,
  specnm text,
  technics text,
  properties jsonb not null default '{}'::jsonb,
  synced_at timestamptz not null default now()
);

create table if not exists forest_resource_stats (
  id bigserial primary key,
  stat_class_id text,
  stat_name text,
  properties jsonb not null default '{}'::jsonb,
  synced_at timestamptz not null default now()
);

create table if not exists app_users (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  provider_user_id text not null,
  email text,
  display_name text,
  avatar_url text,
  profile jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  last_login_at timestamptz not null default now(),
  unique (provider, provider_user_id)
);

create table if not exists user_sessions (
  token_hash text primary key,
  user_id uuid not null references app_users(id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  revoked_at timestamptz
);

create table if not exists user_parcels (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  pnu text,
  address text,
  admin_name text,
  area_ha numeric,
  parcel jsonb not null default '{}'::jsonb,
  last_analysis jsonb not null default '{}'::jsonb,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, pnu)
);

create table if not exists analysis_records (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  user_parcel_id uuid references user_parcels(id) on delete set null,
  pnu text,
  title text not null,
  analysis jsonb not null,
  created_at timestamptz not null default now()
);

create table if not exists work_tasks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  user_parcel_id uuid references user_parcels(id) on delete set null,
  pnu text,
  title text not null,
  category text not null default '현장 확인',
  status text not null default '대기',
  due_date date,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists field_notes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  user_parcel_id uuid references user_parcels(id) on delete set null,
  pnu text,
  note text not null,
  lat numeric,
  lon numeric,
  attachments jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists user_documents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  user_parcel_id uuid references user_parcels(id) on delete set null,
  pnu text,
  name text not null,
  kind text not null default '분석 문서',
  source text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists user_alerts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  user_parcel_id uuid references user_parcels(id) on delete set null,
  pnu text,
  title text not null,
  message text not null,
  level text not null default '안내',
  due_at timestamptz,
  status text not null default '대기',
  created_at timestamptz not null default now()
);

create table if not exists user_shares (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  user_parcel_id uuid references user_parcels(id) on delete cascade,
  share_token text not null unique,
  permission text not null default 'view',
  created_at timestamptz not null default now(),
  expires_at timestamptz
);

create table if not exists analysis_jobs (
  id uuid primary key default gen_random_uuid(),
  pnu text,
  request jsonb not null,
  result jsonb,
  status text not null default 'queued',
  created_at timestamptz not null default now(),
  finished_at timestamptz
);

create table if not exists work_requests (
  id uuid primary key default gen_random_uuid(),
  pnu text,
  address text,
  admin_name text,
  area_ha numeric,
  work_type text not null,
  recommended_scenario text,
  risk_score numeric,
  access_score numeric,
  expected_tasks jsonb not null default '[]'::jsonb,
  quote jsonb not null default '{}'::jsonb,
  analysis jsonb not null default '{}'::jsonb,
  status text not null default '견적 요청',
  created_at timestamptz not null default now()
);

alter table work_requests add column if not exists user_id uuid references app_users(id) on delete set null;
alter table work_requests add column if not exists user_parcel_id uuid references user_parcels(id) on delete set null;

create index if not exists parcels_geom_gix on parcels using gist (geom);
create index if not exists forest_stands_geom_gix on forest_stands using gist (geom);
create index if not exists forest_soils_geom_gix on forest_soils using gist (geom);
create index if not exists planting_zones_geom_gix on planting_zones using gist (geom);
create index if not exists forest_roads_geom_gix on forest_roads using gist (geom);
create index if not exists economic_forest_zones_geom_gix on economic_forest_zones using gist (geom);
create index if not exists landslide_risk_rast_gix on landslide_risk using gist (st_convexhull(rast));
create index if not exists app_users_email_idx on app_users (email);
create index if not exists user_sessions_user_idx on user_sessions (user_id, expires_at desc);
create index if not exists user_parcels_user_idx on user_parcels (user_id, updated_at desc);
create index if not exists analysis_records_user_idx on analysis_records (user_id, created_at desc);
create index if not exists work_tasks_user_idx on work_tasks (user_id, status, due_date);
create index if not exists field_notes_user_idx on field_notes (user_id, created_at desc);
create index if not exists user_documents_user_idx on user_documents (user_id, created_at desc);
create index if not exists user_alerts_user_idx on user_alerts (user_id, status, due_at);
create index if not exists work_requests_region_idx on work_requests (admin_name, created_at desc);
create index if not exists work_requests_scores_idx on work_requests (risk_score desc, access_score asc);
