"""
The diary on paper: a day per block, its meals under it, and what the
period averaged. A quiet sheet — a header, hairlines, numbers in their
columns — typeset in IBM Plex Sans (vendored, OFL).
"""

from __future__ import annotations

import csv
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

_FONTS: dict[str, str] | None = None


def _fonts() -> dict[str, str]:
    """IBM Plex Sans when the vendored files are present, Helvetica otherwise.
    Plex also carries the č, ć, š, ž, đ that food names are written with."""
    global _FONTS
    if _FONTS is not None:
        return _FONTS
    try:
        pdfmetrics.registerFont(TTFont("Plex", str(FONT_DIR / "IBMPlexSans-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Plex-SemiBold", str(FONT_DIR / "IBMPlexSans-SemiBold.ttf")))
        pdfmetrics.registerFont(TTFont("Plex-Bold", str(FONT_DIR / "IBMPlexSans-Bold.ttf")))
        _FONTS = {"regular": "Plex", "semibold": "Plex-SemiBold", "bold": "Plex-Bold"}
    except Exception:  # noqa: BLE001 — a missing font must never sink the export
        _FONTS = {"regular": "Helvetica", "semibold": "Helvetica-Bold", "bold": "Helvetica-Bold"}
    return _FONTS


def _truncate(text: str, font: str, size: float, max_width: float) -> str:
    if stringWidth(text, font, size) <= max_width:
        return text
    while text and stringWidth(text + "…", font, size) > max_width:
        text = text[:-1]
    return text + "…"


PAGE_W, PAGE_H = A4
MARGIN = 40.0
CONTENT_W = PAGE_W - 2 * MARGIN
INK = HexColor("#0F172A")
MUTED = HexColor("#64748B")
RULE = HexColor("#E2E8F0")
BAND = HexColor("#F1F5F9")
ZEBRA = HexColor("#F8FAFC")
GOOD = HexColor("#059669")
WARN = HexColor("#D97706")

# Where each number sits, measured from the margin
COL_WHAT = 0.0
COL_P = 300.0
COL_C = 355.0
COL_F = 410.0
COL_KCAL = CONTENT_W


def _macro_columns(pdf: Canvas, fonts, y: float, values, *, bold=False, color=INK) -> None:
    protein, carbs, fat, kcal = values
    font = "semibold" if bold else "regular"
    for x, value in ((COL_P, protein), (COL_C, carbs), (COL_F, fat)):
        pdf.setFillColor(color)
        pdf.setFont(fonts[font], 8.5)
        pdf.drawRightString(MARGIN + x, y, f"{value:.0f}")
    pdf.setFillColor(color)
    pdf.setFont(fonts["semibold" if bold else "regular"], 8.5)
    pdf.drawRightString(MARGIN + COL_KCAL, y, f"{kcal:,.0f}".replace(",", " "))


def build_pdf(days: list, *, who: str, target, generated_at: datetime) -> bytes:
    """`days` is a list of (day, meals, totals)."""
    fonts = _fonts()
    buffer = BytesIO()
    pdf = Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Eating diary — {who}")

    page = 0

    def text(x, y, value, *, size=9.0, font="regular", color=INK, align="left"):
        pdf.setFillColor(color)
        pdf.setFont(fonts[font], size)
        if align == "right":
            pdf.drawRightString(x, y, value)
        else:
            pdf.drawString(x, y, value)

    def header() -> float:
        nonlocal page
        page += 1
        y = PAGE_H - MARGIN
        text(MARGIN, y - 4, "Nutrijurnal", size=10, font="bold", color=GOOD)
        text(
            MARGIN + CONTENT_W,
            y - 4,
            f"{who}  ·  {generated_at.strftime('%d %b %Y, %H:%M')}",
            size=7.5,
            color=MUTED,
            align="right",
        )
        if page > 1:
            y -= 34
            text(MARGIN, y, "Eating diary", size=10, font="semibold")
            pdf.setStrokeColor(RULE)
            pdf.setLineWidth(0.5)
            pdf.line(MARGIN, y - 9, MARGIN + CONTENT_W, y - 9)
            return y - 28
        y -= 46
        text(MARGIN, y, "Eating diary", size=22, font="bold")
        if days:
            first, last = days[0][0], days[-1][0]
            span = (
                first.strftime("%d %b %Y") if first == last else f"{first:%d %b} – {last:%d %b %Y}"
            )
            text(MARGIN + CONTENT_W, y, span, size=12, color=MUTED, align="right")
        return y - 26

    def footer() -> None:
        text(MARGIN + CONTENT_W, MARGIN - 14, f"Page {page}", size=7, color=MUTED, align="right")

    def column_heads(y: float) -> float:
        for x, label, align in (
            (COL_WHAT, "What", "left"),
            (COL_P, "P", "right"),
            (COL_C, "C", "right"),
            (COL_F, "F", "right"),
            (COL_KCAL, "KCAL", "right"),
        ):
            text(
                MARGIN + x,
                y,
                label.upper(),
                size=6.5,
                font="semibold",
                color=MUTED,
                align=align,
            )
        return y - 12

    y = header()

    if not days:
        text(MARGIN, y - 10, "Nothing written down for this period.", size=10, color=MUTED)
        footer()
        pdf.save()
        return buffer.getvalue()

    # The period at a glance
    count = len(days)
    sums = [
        sum(totals[name] for _, _, totals in days) for name in ("protein", "carbs", "fat", "kcal")
    ]
    averages = [value / count for value in sums]
    pdf.setStrokeColor(INK)
    pdf.setLineWidth(0.8)
    pdf.line(MARGIN, y, MARGIN + CONTENT_W, y)
    cells = [
        ("Days", f"{count}"),
        ("Avg kcal", f"{averages[3]:,.0f}".replace(",", " ")),
        ("Avg protein", f"{averages[0]:.0f} g"),
        ("Avg carbs", f"{averages[1]:.0f} g"),
        ("Avg fat", f"{averages[2]:.0f} g"),
    ]
    cell = CONTENT_W / len(cells)
    for index, (label, value) in enumerate(cells):
        x = MARGIN + index * cell
        text(x, y - 15, label.upper(), size=6.5, font="semibold", color=MUTED)
        colour = INK
        if label == "Avg kcal" and target is not None and target.kcal:
            colour = GOOD if averages[3] <= target.kcal else WARN
        text(x, y - 34, value, size=16, font="bold", color=colour)
    if target is not None and target.kcal:
        text(
            MARGIN + CONTENT_W,
            y - 15,
            f"Target {target.kcal:,.0f} kcal".replace(",", " "),
            size=7.5,
            color=MUTED,
            align="right",
        )
    pdf.setStrokeColor(RULE)
    pdf.setLineWidth(0.5)
    pdf.line(MARGIN, y - 46, MARGIN + CONTENT_W, y - 46)
    y -= 70

    for day, meals, totals in days:
        needed = 46 + 14 * sum(1 + len(meal.items) for meal in meals)
        if y - min(needed, 200) < MARGIN + 20:
            footer()
            pdf.showPage()
            y = header()
        # The day's band
        pdf.setFillColor(BAND)
        pdf.roundRect(MARGIN, y - 6, CONTENT_W, 20, 4, stroke=0, fill=1)
        text(MARGIN + 6, y, day.strftime("%A, %d %B"), size=9.5, font="semibold")
        over = target is not None and target.kcal and totals["kcal"] > target.kcal
        _macro_columns(
            pdf,
            fonts,
            y,
            (totals["protein"], totals["carbs"], totals["fat"], totals["kcal"]),
            bold=True,
            color=WARN if over else INK,
        )
        y -= 24
        y = column_heads(y)

        stripe = False
        for meal in meals:
            when = meal.at.strftime("%H:%M") if meal.at else "—"
            if stripe:
                pdf.setFillColor(ZEBRA)
                pdf.rect(MARGIN, y - 4, CONTENT_W, 14, stroke=0, fill=1)
            stripe = not stripe
            text(MARGIN, y, when, size=8, font="semibold", color=MUTED)
            text(
                MARGIN + 34,
                y,
                _truncate(meal.title, fonts["semibold"], 8.5, COL_P - 44),
                size=8.5,
                font="semibold",
            )
            _macro_columns(
                pdf, fonts, y, (meal.protein, meal.carbs, meal.fat, meal.kcal), bold=True
            )
            y -= 13
            for item in meal.items:
                amount = (
                    f"{item.grams:g} g"
                    if item.unit in ("g", "ml")
                    else f"{item.quantity:g} × {item.unit} ({item.grams:g} g)"
                )
                line = _truncate(f"{item.label} · {amount}", fonts["regular"], 7.5, COL_P - 54)
                text(MARGIN + 44, y, line, size=7.5, color=MUTED)
                _macro_columns(
                    pdf, fonts, y, (item.protein, item.carbs, item.fat, item.kcal), color=MUTED
                )
                y -= 11
            y -= 2
            if y < MARGIN + 30:
                footer()
                pdf.showPage()
                y = header()
        pdf.setStrokeColor(RULE)
        pdf.setLineWidth(0.5)
        pdf.line(MARGIN, y + 4, MARGIN + CONTENT_W, y + 4)
        y -= 16

    footer()
    pdf.save()
    return buffer.getvalue()


def build_csv(days: list) -> bytes:
    """One row per item, with the meal and the day repeated — a shape a
    spreadsheet can pivot without any cleaning up."""
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "Day",
            "Time",
            "Meal",
            "Item",
            "Quantity",
            "Unit",
            "Grams",
            "Protein",
            "Carbs",
            "Fat",
            "Kcal",
        ]
    )
    for day, meals, totals in days:
        for meal in meals:
            for item in meal.items:
                writer.writerow(
                    [
                        day.isoformat(),
                        meal.at.strftime("%H:%M") if meal.at else "",
                        meal.title,
                        item.label,
                        f"{item.quantity:g}",
                        item.unit,
                        f"{item.grams:g}",
                        f"{item.protein:.1f}",
                        f"{item.carbs:.1f}",
                        f"{item.fat:.1f}",
                        f"{item.kcal:.0f}",
                    ]
                )
        writer.writerow(
            [
                day.isoformat(),
                "",
                "DAY TOTAL",
                "",
                "",
                "",
                "",
                f"{totals['protein']:.1f}",
                f"{totals['carbs']:.1f}",
                f"{totals['fat']:.1f}",
                f"{totals['kcal']:.0f}",
            ]
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")
