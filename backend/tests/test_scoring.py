from app.scoring import FeatureSet, distance_score, score_features
from app.public_clients import _current_admin_code


def test_distance_score_declines_with_distance():
    assert distance_score(50) == 100
    assert distance_score(2100) == 20
    assert distance_score(500) > distance_score(1500)


def test_profit_scenario_with_good_access_and_economic_zone():
    scores = score_features(
        FeatureSet(
            area_ha=8,
            road_distance_m=120,
            road_density_m_per_ha=18,
            slope_degree=11,
            avg_landslide_grade=4,
            high_landslide_ratio=0.05,
            economic_forest=True,
            planting_fit_count=4,
            stand_age_class=5,
        )
    )
    assert scores["profit"] > scores["conservation"]
    assert scores["recommendedScenario"] in {"수익형 경영", "탄소형 경영", "재난저감형 경영"}


def test_current_admin_code_updates_special_province_codes():
    assert _current_admin_code("42760", 5) == "51760"
    assert _current_admin_code("45710", 5) == "52710"
    assert _current_admin_code("51760", 5) == "51760"
