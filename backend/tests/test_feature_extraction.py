from app.main import (
    STAND_AGE_KEYS,
    STAND_SPECIES_KEYS,
    _extract_int_property,
    _extract_slope_degree,
    _extract_text_property,
    _json_object,
    _valid_pnu,
)


def test_extracts_stand_age_and_species_from_public_data_keys():
    properties = {"AGCLS_CD": "4", "KOFTR_GROUP_CD": "2"}

    assert _extract_int_property(properties, STAND_AGE_KEYS) == 4
    assert _extract_text_property(properties, STAND_SPECIES_KEYS) == "활엽수림"


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
