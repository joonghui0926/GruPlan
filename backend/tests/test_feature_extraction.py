from app.main import (
    STAND_AGE_KEYS,
    STAND_SPECIES_KEYS,
    _extract_int_property,
    _extract_slope_degree,
    _extract_text_property,
    _extract_vworld_fire_risk_index,
    _fgis_economic_forest,
    _fgis_landslide_grade,
    _fgis_planting_count,
    _first_vworld_feature,
    _first_supermap_feature,
    _merge_client_features,
    _merge_fgis_features,
    _lonlat_to_epsg5179,
    _supermap_geometry_to_geojson,
    _json_object,
    _valid_pnu,
)


def test_extracts_stand_age_and_species_from_public_data_keys():
    properties = {"AGCLS_CD": "4", "KOFTR_GROUP_CD": "2"}

    assert _extract_int_property(properties, STAND_AGE_KEYS) == 4
    assert _extract_text_property(properties, STAND_SPECIES_KEYS) == "활엽수림"


def test_extracts_extended_stand_fields():
    properties = {"AGECLS_CD": "6", "KOFTR_CD1": "42"}

    assert _extract_int_property(properties, STAND_AGE_KEYS) == 6
    assert "42" in _extract_text_property(properties, STAND_SPECIES_KEYS)


def test_extracts_stand_names_from_nm_fields():
    properties = {"AGCLS_NM": "5영급", "KOFTR_NM": "소나무"}

    assert _extract_int_property(properties, STAND_AGE_KEYS) == 5
    assert _extract_text_property(properties, STAND_SPECIES_KEYS) == "소나무"


def test_extracts_korean_stand_fields():
    properties = {"영급": "5영급", "수종": "소나무"}

    assert _extract_int_property(properties, STAND_AGE_KEYS) == 5
    assert _extract_text_property(properties, STAND_SPECIES_KEYS) == "소나무"


def test_converts_slope_class_to_representative_degree():
    properties = {"SLANT_CD": "4"}

    assert _extract_slope_degree(properties) == 22.5
    assert _extract_slope_degree({"경사도": "18.2"}) == 18.2


def test_accepts_jsonb_strings_from_database_driver():
    assert _json_object('{"stand":{"AGCLS_CD":"4"}}') == {"stand": {"AGCLS_CD": "4"}}
    assert _json_object("not json") == {}


def test_validates_pnu_before_database_lookup():
    assert _valid_pnu("5176033024200010000") == "5176033024200010000"
    assert _valid_pnu("123") is None
    assert _valid_pnu("gid-42") is None


def test_extracts_first_vworld_feature():
    data = {
        "response": {
            "result": {
                "featureCollection": {
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"pnu": "4886037025200660000"},
                            "geometry": {"type": "Polygon", "coordinates": []},
                        }
                    ]
                }
            }
        }
    }

    assert _first_vworld_feature(data)["properties"]["pnu"] == "4886037025200660000"


def test_merges_client_vworld_features():
    merged = _merge_client_features({}, {"soil": {"properties": {"name": "갈색건조산림토양"}}})

    assert merged["soil"]["name"] == "갈색건조산림토양"
    assert merged["soilMatch"]["matchType"] == "VWorld 2D 데이터"


def test_extracts_vworld_fire_risk_index():
    properties = {"value00h": "30", "value01h": "42", "value02h": "0"}

    assert _extract_vworld_fire_risk_index(properties) == 42


def test_converts_wgs84_to_epsg5179_for_fgis():
    x, y = _lonlat_to_epsg5179(127.887, 35.318)

    assert round(x, 1) == 1035177.3
    assert round(y, 1) == 1702562.6


def test_converts_supermap_region_to_geojson():
    geometry = {
        "type": "REGION",
        "parts": [4],
        "points": [
            {"x": 0, "y": 0},
            {"x": 1, "y": 0},
            {"x": 1, "y": 1},
            {"x": 0, "y": 0},
        ],
    }

    assert _supermap_geometry_to_geojson(geometry) == {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
    }


def test_extracts_supermap_feature_properties():
    data = {
        "features": [
            {
                "ID": 944545,
                "fieldNames": ["SMID", "AGCLS_NM", "KOFTR_NM"],
                "fieldValues": ["944545", "5영급", "소나무"],
                "geometry": {"type": "POINT", "parts": [1], "points": [{"x": 1, "y": 2}]},
            }
        ]
    }

    feature = _first_supermap_feature(data, "TB_FGDI_IM5000")

    assert feature["id"] == "fgis:TB_FGDI_IM5000:944545"
    assert feature["properties"]["AGCLS_NM"] == "5영급"
    assert feature["properties"]["KOFTR_NM"] == "소나무"


def test_merges_fgis_features_and_derives_counts():
    fgis = {
        "stand": {"properties": {"AGCLS_NM": "5영급", "KOFTR_NM": "소나무"}},
        "soilFgis": {"properties": {"SLANT_TYP": "2"}},
        "planting5000": {"properties": {"RPRSN_KOFTR": "가능지"}},
        "planting25000": {"properties": {"KOFTR_NM": "소나무"}},
        "economicPrivate": {"properties": {"MAPID_NM": "단성"}},
        "landslideVector": {"properties": {"GRIDCODE": "4"}},
    }

    merged = _merge_fgis_features({}, fgis)

    assert merged["stand"]["KOFTR_NM"] == "소나무"
    assert merged["soil"]["SLANT_TYP"] == "2"
    assert _fgis_planting_count(fgis) == 2
    assert _fgis_economic_forest(fgis) is True
    assert _fgis_landslide_grade(fgis) == 4
