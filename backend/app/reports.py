from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


COVER_GREEN = "#18351c"
LINE_GREEN = "#c7d4c0"
LIGHT_GREEN = "#edf4ea"


def _register_korean_font() -> str:
    candidates = [
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/NanumGothic.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for font in candidates:
        if font.exists():
            pdfmetrics.registerFont(TTFont("GruplanBody", str(font)))
            return "GruplanBody"
    return "Helvetica"


def build_plan_pdf(analysis: dict) -> bytes:
    font_name = _register_korean_font()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CoverBrand", fontName=font_name, fontSize=14, leading=18, textColor=colors.white, spaceAfter=18))
    styles.add(ParagraphStyle(name="CoverTitle", fontName=font_name, fontSize=30, leading=38, textColor=colors.white, spaceAfter=8))
    styles.add(ParagraphStyle(name="CoverSubtitle", fontName=font_name, fontSize=14, leading=22, textColor=colors.HexColor("#dfeede"), spaceAfter=22))
    styles.add(ParagraphStyle(name="CoverBody", fontName=font_name, fontSize=10, leading=16, textColor=colors.HexColor("#dfeede")))
    styles.add(ParagraphStyle(name="CoverSummary", fontName=font_name, fontSize=11, leading=18, textColor=colors.white, leftIndent=0, spaceAfter=7))
    styles.add(ParagraphStyle(name="KoTitle", fontName=font_name, fontSize=18, leading=24, spaceAfter=10, textColor=colors.HexColor(COVER_GREEN)))
    styles.add(ParagraphStyle(name="KoHeading", fontName=font_name, fontSize=12, leading=17, spaceBefore=10, spaceAfter=6, textColor=colors.HexColor(COVER_GREEN)))
    styles.add(ParagraphStyle(name="KoBody", fontName=font_name, fontSize=9, leading=14, textColor=colors.HexColor("#203024")))

    parcel = analysis.get("parcel", {})
    scores = analysis.get("scores", {})
    xai = analysis.get("xai", {})
    sources = analysis.get("sources", [])
    tasks = analysis.get("workPlan", [])
    features = analysis.get("features", {})
    scenario_plan = analysis.get("scenarioPlan") or {}
    scenario_reasons = analysis.get("scenarioReasons") or []
    narrative = str(analysis.get("narrative") or "").strip()
    scenario = str(scores.get("recommendedScenario") or "확인 필요")
    area = parcel.get("areaHa", "확인 필요")
    area_text = "확인 필요" if area == "확인 필요" else f"{area} ha"
    address = str(parcel.get("address") or parcel.get("pnu") or "선택 필지")

    story = [
        Spacer(1, 64),
        Paragraph("GruPlan AI", styles["CoverBrand"]),
        Paragraph("산림경영 분석 리포트", styles["CoverTitle"]),
        Paragraph("필지 단위 산림자산 진단과 경영 방향 제안", styles["CoverSubtitle"]),
        Spacer(1, 52),
        Paragraph(f"대상 필지&nbsp;&nbsp; {escape(address)}", styles["CoverSummary"]),
        Paragraph(f"추천 방향&nbsp;&nbsp; {escape(scenario)}", styles["CoverSummary"]),
        Paragraph(f"분석 면적&nbsp;&nbsp; {escape(str(area_text))}", styles["CoverSummary"]),
        Spacer(1, 24),
        Paragraph("필지 경계, 산림공간정보, 재난 위험, 탄소 가능성, 실행 일정을 한 문서로 정리했습니다.", styles["CoverBody"]),
        Spacer(1, 250),
        Paragraph("공공데이터 기반 참고 문서입니다. 행정 제출 전 산림기술자 또는 담당 기관 검토가 필요합니다.", styles["CoverBody"]),
        PageBreak(),
        Paragraph("산림경영계획서 초안", styles["KoTitle"]),
        Paragraph("필지 경계, 공간 데이터, 시나리오 점수, XAI 근거 체인을 정리한 참고 문서입니다.", styles["KoBody"]),
        Spacer(1, 10),
    ]

    parcel_rows = [["항목", "내용"]]
    for label, key in [("PNU", "pnu"), ("주소", "address"), ("면적", "areaHa"), ("행정구역", "adminName")]:
        parcel_rows.append([label, str(parcel.get(key, "확인 필요"))])
    story.append(Table(parcel_rows, colWidths=[32 * mm, 126 * mm], style=_table_style(font_name)))

    if narrative:
        story.append(Paragraph("경영 방향 요약", styles["KoHeading"]))
        for paragraph in narrative.splitlines():
            paragraph = paragraph.strip()
            if paragraph:
                story.append(Paragraph(escape(paragraph), styles["KoBody"]))
                story.append(Spacer(1, 3))

    if scenario_plan:
        story.append(Paragraph("추천 시나리오 설계", styles["KoHeading"]))
        story.append(_para(scenario_plan.get("thesis", ""), styles))
        plan_rows = [["구분", "내용"]]
        plan_rows.append(["작용 근거", _para(_list_text(scenario_plan.get("context") or []), styles)])
        plan_rows.append(["운영 아이디어", _para(_list_text(scenario_plan.get("ideas") or []), styles)])
        phase_text = []
        for phase in scenario_plan.get("phases") or []:
            phase_text.append(f"{phase.get('name', '')}: " + " / ".join(phase.get("actions") or []))
        plan_rows.append(["실행 단계", _para(_list_text(phase_text), styles)])
        plan_rows.append(["주의할 점", _para(_list_text(scenario_plan.get("risks") or []), styles)])
        story.append(Table(plan_rows, colWidths=[32 * mm, 126 * mm], style=_table_style(font_name, font_size=7, leading=10)))

    if scenario_reasons:
        story.append(Paragraph("시나리오 비교 근거", styles["KoHeading"]))
        rows = [["방향", "점수", "이 필지에서 그렇게 나온 이유", "작용값", "다음 확인"]]
        for item in scenario_reasons:
            rows.append([
                item.get("scenario", ""),
                str(round(item.get("score") or 0)),
                _para(item.get("judgement", ""), styles),
                _para(_list_text(item.get("drivers") or []), styles),
                _para(item.get("nextCheck", ""), styles),
            ])
        story.append(Table(rows, colWidths=[18 * mm, 15 * mm, 58 * mm, 32 * mm, 35 * mm], style=_table_style(font_name, font_size=7, leading=10)))

    story.append(Paragraph("시나리오 점수", styles["KoHeading"]))
    score_rows = [["지표", "점수"]]
    for key, label in [
        ("accessibility", "접근성"),
        ("disasterRisk", "재난위험"),
        ("profit", "수익형"),
        ("carbon", "탄소형"),
        ("conservation", "보전형"),
        ("resilience", "재난저감형"),
    ]:
        score_rows.append([label, str(scores.get(key, "계산 필요"))])
    score_rows.append(["추천 시나리오", scores.get("recommendedScenario", "확인 필요")])
    story.append(Table(score_rows, colWidths=[60 * mm, 98 * mm], style=_table_style(font_name)))

    feature_rows = _feature_rows(features)
    if feature_rows:
        story.append(Paragraph("산림공간 원천 지표", styles["KoHeading"]))
        rows = [["분류", "속성", "값"]]
        for group, key, value in feature_rows:
            rows.append([group, key, _para(value, styles)])
        story.append(Table(rows, colWidths=[30 * mm, 48 * mm, 80 * mm], style=_table_style(font_name, font_size=7, leading=10)))

    explanations = xai.get("scoreExplanations") or []
    if explanations:
        story.append(Paragraph("XAI 점수 근거", styles["KoHeading"]))
        rows = [["지표", "점수 산식", "출처", "입력값", "해석"]]
        for item in explanations:
            rows.append([
                _para(f"{item.get('metric', '')}<br/>{item.get('score', '확인 필요')}점", styles),
                _para(item.get("formula", ""), styles),
                _para(", ".join(item.get("sourceIds", [])), styles),
                _para(_input_summary(item.get("inputs") or {}), styles),
                _para(item.get("interpretation", ""), styles),
            ])
        story.append(Table(rows, colWidths=[21 * mm, 38 * mm, 19 * mm, 35 * mm, 45 * mm], style=_table_style(font_name, font_size=7, leading=10)))

    chain = xai.get("retrievalChain") or []
    if chain:
        story.append(Paragraph("공공데이터 근거 체인", styles["KoHeading"]))
        rows = [["순서", "단계", "출처", "근거"]]
        for index, item in enumerate(chain, start=1):
            rows.append([
                str(index),
                _para(item.get("step", ""), styles),
                _para(", ".join(item.get("sourceIds", [])), styles),
                _para(item.get("evidence", ""), styles),
            ])
        story.append(Table(rows, colWidths=[14 * mm, 34 * mm, 36 * mm, 74 * mm], style=_table_style(font_name, font_size=7, leading=10)))

    story.append(Paragraph("올해 작업 체크리스트", styles["KoHeading"]))
    if tasks:
        rows = [["작업", "근거", "시기"]]
        for item in tasks:
            rows.append([item.get("title", ""), item.get("reason", ""), item.get("season", "")])
    else:
        rows = [["작업", "근거", "시기"], ["공간분석 완료 후 생성", "원천 데이터 적재 필요", "확인 필요"]]
    story.append(Table(rows, colWidths=[45 * mm, 78 * mm, 35 * mm], style=_table_style(font_name)))

    story.append(Paragraph("근거 데이터", styles["KoHeading"]))
    source_rows = [["ID", "데이터", "상태"]]
    for source in sources:
        source_rows.append([source.get("id", ""), source.get("name", ""), source.get("status", "")])
    story.append(Table(source_rows, colWidths=[18 * mm, 102 * mm, 38 * mm], style=_table_style(font_name)))

    doc.build(story, onFirstPage=_cover_page)
    return buffer.getvalue()


def _para(value, styles):
    escaped = escape(str(value or "")).replace("&lt;br/&gt;", "<br/>")
    return Paragraph(escaped, styles["KoBody"])


def _feature_rows(features: dict) -> list[tuple[str, str, str]]:
    rows = []
    for group, values in [("임상도", features.get("stand")), ("입지토양", features.get("soil"))]:
        if not isinstance(values, dict):
            continue
        for key, value in list(values.items())[:8]:
            if value in (None, "") or "geom" in str(key).lower():
                continue
            rows.append((group, str(key), _format_feature_value(value)))
    return rows


def _input_summary(inputs: dict) -> str:
    rows = []
    for key, value in inputs.items():
        if value in (None, ""):
            continue
        rows.append(f"{key}: {_format_feature_value(value)}")
    return "<br/>".join(rows) if rows else "원천값 확인"


def _list_text(items: list) -> str:
    return "<br/>".join(str(item) for item in items if item)


def _format_feature_value(value) -> str:
    if isinstance(value, bool):
        return "해당" if value else "미해당"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _cover_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(colors.HexColor(COVER_GREEN))
    width, height = A4
    canvas.rect(0, 0, width, height, stroke=0, fill=1)
    canvas.restoreState()


def _table_style(font_name: str, font_size: int = 8, leading: int = 11) -> TableStyle:
    return TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), font_size),
            ("LEADING", (0, 0), (-1, -1), leading),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(LIGHT_GREEN)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(COVER_GREEN)),
            ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor(LINE_GREEN)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )
