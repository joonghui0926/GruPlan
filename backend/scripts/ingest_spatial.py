from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


SOURCE_TABLES = {
    "D1": ("forest_stands", "vector"),
    "D2": ("forest_soils", "vector"),
    "D3": ("planting_zones", "vector"),
    "D4": ("forest_roads", "vector"),
    "D5": ("landslide_risk", "raster"),
    "D8": ("economic_forest_zones", "vector"),
    "D12": ("parcels", "vector"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Load real public forest spatial data into PostGIS.")
    parser.add_argument("--source", required=True, choices=SOURCE_TABLES.keys())
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--source-srs", default=None, help="원본 좌표계. 예: EPSG:5179, EPSG:5186")
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL이 필요합니다.")
    if not args.file.exists():
        raise SystemExit(f"파일을 찾을 수 없습니다: {args.file}")

    table, kind = SOURCE_TABLES[args.source]
    if kind == "vector":
        load_vector(args.database_url, args.file, table, args.source_srs, args.append)
    else:
        load_raster(args.database_url, args.file, table, args.source_srs)


def load_vector(database_url: str, path: Path, table: str, source_srs: str | None, append: bool) -> None:
    mode = "-append" if append else "-overwrite"
    command = [
        "ogr2ogr",
        "-f",
        "PostgreSQL",
        f"PG:{database_url}",
        str(path),
        "-nln",
        table,
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "GEOMETRY_NAME=geom",
        "-lco",
        "FID=id",
        "-t_srs",
        "EPSG:4326",
        mode,
    ]
    if source_srs:
        command.extend(["-s_srs", source_srs])
    subprocess.run(command, check=True)


def load_raster(database_url: str, path: Path, table: str, source_srs: str | None) -> None:
    srid = "5186"
    if source_srs and source_srs.upper().startswith("EPSG:"):
        srid = source_srs.split(":", 1)[1]
    raster = subprocess.Popen(
        ["raster2pgsql", "-s", srid, "-t", "256x256", "-I", "-C", "-M", str(path), table],
        stdout=subprocess.PIPE,
    )
    subprocess.run(["psql", database_url], stdin=raster.stdout, check=True)
    raster.wait()


if __name__ == "__main__":
    main()
