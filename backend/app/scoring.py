from __future__ import annotations

from dataclasses import dataclass


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def distance_score(meters: float | None) -> float:
    if meters is None:
        return 45
    if meters <= 100:
        return 100
    if meters >= 2000:
        return 20
    return clamp(100 - ((meters - 100) / 1900) * 80)


def slope_penalty(slope_degree: float | None) -> float:
    if slope_degree is None:
        return 0
    if slope_degree <= 15:
        return 0
    if slope_degree >= 35:
        return 35
    return (slope_degree - 15) * 1.75


def landslide_risk_score(avg_grade: float | None, high_risk_ratio: float | None) -> float:
    if avg_grade is None and high_risk_ratio is None:
        return 45
    grade = avg_grade if avg_grade is not None else 5
    ratio = high_risk_ratio if high_risk_ratio is not None else 0
    grade_component = clamp((6 - grade) * 18)
    ratio_component = clamp(ratio * 100) * 0.45
    return clamp(grade_component + ratio_component)


@dataclass
class FeatureSet:
    area_ha: float | None = None
    road_distance_m: float | None = None
    road_density_m_per_ha: float | None = None
    slope_degree: float | None = None
    avg_landslide_grade: float | None = None
    high_landslide_ratio: float | None = None
    fire_risk_index: float | None = None
    economic_forest: bool = False
    planting_fit_count: int = 0
    stand_age_class: int | None = None
    stand_species: str | None = None
    carbon_case_similarity: float | None = None


def score_features(features: FeatureSet) -> dict:
    access = distance_score(features.road_distance_m) - slope_penalty(features.slope_degree)
    if features.road_density_m_per_ha is not None:
        access += clamp(features.road_density_m_per_ha * 8, 0, 18)

    disaster = landslide_risk_score(features.avg_landslide_grade, features.high_landslide_ratio)
    if features.fire_risk_index is not None:
        disaster = clamp(disaster * 0.65 + features.fire_risk_index * 0.35)

    stand_age = features.stand_age_class or 0
    productivity = 40 + (24 if features.economic_forest else 0) + min(features.planting_fit_count * 8, 24)
    productivity += min(stand_age * 2.2, 14)

    access_value = clamp(access)
    profit = productivity * 0.50 + access_value * 0.38 + (100 - disaster) * 0.12
    carbon_base = 32 + min(stand_age * 3.5, 22) + min((features.area_ha or 0) * 1.6, 14)
    if features.carbon_case_similarity is not None:
        carbon_base += features.carbon_case_similarity * 22
    if disaster > 55:
        carbon_base -= 8
    if access_value < 25:
        carbon_base -= 4
    conservation = disaster * 0.58 + slope_penalty(features.slope_degree) * 1.1
    resilience = disaster * 0.62 + (100 - access_value) * 0.20 + min((features.area_ha or 0) * 2, 18)

    scores = {
        "accessibility": round(clamp(access), 1),
        "disasterRisk": round(clamp(disaster), 1),
        "profit": round(clamp(profit), 1),
        "carbon": round(clamp(carbon_base), 1),
        "conservation": round(clamp(conservation), 1),
        "resilience": round(clamp(resilience), 1),
    }
    known = {
        "필지 면적": features.area_ha is not None,
        "임도 거리": features.road_distance_m is not None,
        "경사": features.slope_degree is not None,
        "산사태 위험": features.avg_landslide_grade is not None or features.high_landslide_ratio is not None,
        "영급": features.stand_age_class is not None,
        "수종": bool(features.stand_species),
    }
    evidence_coverage = round(sum(1 for value in known.values() if value) / len(known) * 100)
    gaps = [label for label, present in known.items() if not present]
    scores["evidenceCoverage"] = evidence_coverage
    scores["dataGaps"] = gaps
    scores["needsFieldCheck"] = evidence_coverage < 60
    scenario = max(
        {
            "수익형 경영": scores["profit"],
            "탄소형 경영": scores["carbon"],
            "보전형 경영": scores["conservation"],
            "재난저감형 경영": scores["resilience"],
        },
        key=lambda key: {
            "수익형 경영": scores["profit"],
            "탄소형 경영": scores["carbon"],
            "보전형 경영": scores["conservation"],
            "재난저감형 경영": scores["resilience"],
        }[key],
    )
    scores["recommendedScenario"] = "기초조사 우선" if evidence_coverage < 45 else scenario
    return scores
