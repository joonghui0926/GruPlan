from __future__ import annotations

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


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
    styles.add(ParagraphStyle(name="KoTitle", fontName=font_name, fontSize=18, leading=24, spaceAfter=10))
    styles.add(ParagraphStyle(name="KoHeading", fontName=font_name, fontSize=12, leading=17, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="KoBody", fontName=font_name, fontSize=9, leading=14))

    parcel = analysis.get("parcel", {})
    scores = analysis.get("scores", {})
    sources = analysis.get("sources", [])
    tasks = analysis.get("workPlan", [])
    narrative = str(analysis.get("narrative") or "").strip()

    story = [
        Paragraph("GruPlan 산림경영계획서 초안", styles["KoTitle"]),
        Paragraph("행정 제출 전 산림기술자 또는 담당 기관의 검토가 필요한 참고 문서입니다.", styles["KoBody"]),
        Spacer(1, 8),
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

    doc.build(story)
    return buffer.getvalue()


def _table_style(font_name: str) -> TableStyle:
    return TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LEADING", (0, 0), (-1, -1), 11),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf4ea")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#18351c")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c7d4c0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )
