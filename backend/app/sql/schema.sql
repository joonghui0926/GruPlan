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

create index if not exists parcels_geom_gix on parcels using gist (geom);
create index if not exists forest_stands_geom_gix on forest_stands using gist (geom);
create index if not exists forest_soils_geom_gix on forest_soils using gist (geom);
create index if not exists planting_zones_geom_gix on planting_zones using gist (geom);
create index if not exists forest_roads_geom_gix on forest_roads using gist (geom);
create index if not exists economic_forest_zones_geom_gix on economic_forest_zones using gist (geom);
create index if not exists landslide_risk_rast_gix on landslide_risk using gist (st_convexhull(rast));
create index if not exists work_requests_region_idx on work_requests (admin_name, created_at desc);
create index if not exists work_requests_scores_idx on work_requests (risk_score desc, access_score asc);
