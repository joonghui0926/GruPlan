from app.llm import fallback_plan_narrative, _extract_output_text
from app.main import _number_or_none


def test_extract_output_text_from_responses_shape():
    data = {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": "첫 문단"},
                    {"type": "output_text", "text": "둘째 문단"},
                ]
            }
        ]
    }

    assert _extract_output_text(data) == "첫 문단\n둘째 문단"


def test_fallback_plan_narrative_uses_analysis_values():
    text = fallback_plan_narrative(
        {
            "parcel": {"address": "강원특별자치도 평창군", "areaHa": 4.2},
            "scores": {"recommendedScenario": "탄소형 경영", "accessibility": 72.1, "disasterRisk": 18.4},
            "workPlan": [{"title": "조림 후보 수종 확인"}],
            "sources": [{"name": "산림청_임상도 산림공간정보"}],
        }
    )

    assert "강원특별자치도 평창군" in text
    assert "탄소형 경영" in text
    assert "조림 후보 수종 확인" in text


def test_number_or_none_converts_numeric_text():
    assert _number_or_none("12.5") == 12.5
    assert _number_or_none("") is None
    assert _number_or_none("값 없음") is None
