from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any
import hashlib

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem


def _safe(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _dynamic_match_score(report: dict, branch: dict, idx: int) -> int:
    for key in ("match_score", "score", "confidence"):
        raw = branch.get(key)
        if isinstance(raw, (int, float)):
            return int(max(60, min(99, round(raw))))
    seed = f"{_safe(branch.get('branch'))}-{_safe(report.get('summary'))}-{idx}"
    digest = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
    jitter = (digest % 5) - 2  # -2..2
    base = 93 - (idx - 1) * 6
    score = base + jitter
    if idx == 1 and score < 85:
        score = 85
    if idx == 3 and score > 88:
        score = 88
    return int(max(74, min(96, score)))


def build_report_pdf(report: dict, user_name: str, user_email: str, assessment_id: int | None) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=48,
        rightMargin=48,
        topMargin=48,
        bottomMargin=48,
        title="A.GCareerSathi Recommendation Report",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontSize=20,
        alignment=1,
        textColor=colors.HexColor("#111827"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["BodyText"],
        fontSize=11,
        alignment=1,
        textColor=colors.HexColor("#6b7280"),
        spaceAfter=14,
    )
    badge_style = ParagraphStyle(
        "Badge",
        parent=styles["BodyText"],
        fontSize=9,
        alignment=0,
        textColor=colors.HexColor("#0f766e"),
        spaceAfter=6,
    )
    heading_style = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=colors.HexColor("#111827"),
        spaceBefore=6,
        spaceAfter=4,
    )
    label_style = ParagraphStyle(
        "Label",
        parent=styles["BodyText"],
        fontSize=9,
        textColor=colors.HexColor("#6b7280"),
        spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#111827"),
    )

    pageWidth, pageHeight = A4
    margin = 48
    flowables: list[Any] = []
    header = Table(
        [[Paragraph("A.GCareerSathi Recommendation Report", ParagraphStyle(
            "HeaderTitle",
            parent=styles["Title"],
            fontSize=16,
            textColor=colors.white,
            alignment=0,
        ))]],
        colWidths=[pageWidth - margin * 2],
    )
    header.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#0b2b33")),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    flowables.append(header)
    flowables.append(Spacer(1, 0.16 * inch))

    meta_style = ParagraphStyle(
        "Meta",
        parent=styles["BodyText"],
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#111827"),
        spaceAfter=4,
    )
    flowables.append(Paragraph(f"Student: {_safe(user_name)}", meta_style))
    flowables.append(Paragraph(f"Email: {_safe(user_email)}", meta_style))
    if assessment_id is not None:
        flowables.append(Paragraph(f"Assessment ID: {assessment_id}", meta_style))
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    flowables.append(Paragraph(f"Generated: {now.month}/{now.day}/{now.year}, {now.strftime('%I:%M:%S %p')}", meta_style))
    flowables.append(Spacer(1, 0.16 * inch))

    summary = _safe(report.get("summary", ""))
    if summary:
        flowables.append(Paragraph("Summary", heading_style))
        flowables.append(Paragraph(summary, body_style))
        flowables.append(Spacer(1, 0.16 * inch))

    top_branches = report.get("top_branches", []) or []
    for idx, branch in enumerate(top_branches, start=1):
        flowables.append(Spacer(1, 0.12 * inch))
        flowables.append(Paragraph(f"Recommended Branch {idx}: {_safe(branch.get('branch'))}", heading_style))

        why_fit = _safe(branch.get("why_fit"))
        skills = branch.get("courses", []) or []
        roadmap = branch.get("actions", []) or []
        careers = branch.get("careers", []) or []

        items = [
            f"<b>Why fit:</b> {why_fit}",
            f"<b>Courses:</b> {', '.join([_safe(x) for x in skills])}",
            f"<b>Careers:</b> {', '.join([_safe(x) for x in careers])}",
            f"<b>Actions:</b> {', '.join([_safe(x) for x in roadmap])}",
            f"<b>Outlook:</b> {_safe(branch.get('outlook'))}",
            f"<b>Demand:</b> {_safe(branch.get('demand'))}",
            f"<b>Salary range:</b> {_safe(branch.get('salary_range'))}",
        ]
        bullets = ListFlowable(
            [ListItem(Paragraph(item, body_style)) for item in items],
            bulletType="bullet",
            leftIndent=16,
        )
        flowables.append(bullets)

    next_steps = report.get("next_steps", []) or []
    if next_steps:
        flowables.append(Spacer(1, 0.2 * inch))
        flowables.append(Paragraph("Next Steps", heading_style))
        bullets = [ListItem(Paragraph(_safe(item), body_style)) for item in next_steps]
        flowables.append(ListFlowable(bullets, bulletType="1", leftIndent=16))

    scholarships = report.get("scholarships", []) or []
    if scholarships:
        flowables.append(Spacer(1, 0.2 * inch))
        flowables.append(Paragraph("Scholarships & Exams", heading_style))
        bullets = [ListItem(Paragraph(_safe(item), body_style)) for item in scholarships]
        flowables.append(ListFlowable(bullets, bulletType="1", leftIndent=16))

    flowables.append(Spacer(1, 0.25 * inch))
    flowables.append(
        Paragraph(
            "Disclaimer: This report is AI-generated for guidance only. It does not guarantee outcomes and should not be the sole basis for critical decisions. Consider consulting a qualified counselor before making final choices.",
            label_style,
        )
    )

    doc.build(flowables)
    return buffer.getvalue()
