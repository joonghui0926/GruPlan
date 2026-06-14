from app.main import (
    STAND_AGE_KEYS,
    STAND_SPECIES_KEYS,
    _extract_int_property,
    _extract_slope_degree,
    _extract_text_property,
    _extract_vworld_fire_risk_index,
    _first_vworld_feature,
    _merge_client_features,
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
