from app.main import (
    STAND_AGE_KEYS,
    STAND_SPECIES_KEYS,
    _extract_int_property,
    _extract_slope_degree,
    _extract_text_property,
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
