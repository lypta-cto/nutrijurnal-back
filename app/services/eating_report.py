"""
The diary on paper. Page one is the period at a glance — the averages against
the targets, energy a day, where it came from, protein a day — and then the
diary itself, a day per block with its meals under it. The look follows the
app: Apple's greys, basil as the one accent, one colour per macro, typeset in
IBM Plex Sans (vendored, OFL).
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
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
LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo-mark.png"

# The app's own palette (nutrijurnal/app/assets/css/main.css), so paper and
# phone look like one product: Apple's greys, basil as the one accent, and one
# colour per macro
INK = HexColor("#1C1C1E")
MUTED = HexColor("#6E6E73")
FAINT = HexColor("#AEAEB2")
RULE = HexColor("#E5E5EA")
BAND = HexColor("#F2F2F7")
ACCENT = HexColor("#1D7F43")
KCAL = HexColor("#2FB463")
OVER = HexColor("#E9A23B")
OVER_INK = HexColor("#9C5C00")
MACROS = (
    ("protein", "Protein", HexColor("#4A8FE7"), HexColor("#2563C9"), 4),
    ("carbs", "Carbs", HexColor("#8E7CE8"), HexColor("#6A55D4"), 4),
    ("fat", "Fat", HexColor("#E9A23B"), HexColor("#9C5C00"), 9),
)

# Where each number sits in the day-by-day table, measured from the margin
COL_P = 330.0
COL_C = 385.0
COL_F = 440.0
COL_KCAL = CONTENT_W


def _thousands(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _nice_top(highest: float) -> tuple[float, float]:
    """A round top for a chart's axis and the step between its gridlines. The
    top rounds to half a step, so 2 840 kcal tops out at 3 500, not 4 000."""
    highest = max(highest, 1.0)
    for step in (50, 100, 250, 500, 1000, 2000, 5000):
        if highest / step <= 4:
            half = step / 2
            return half * -(-highest // half), step
    return highest, highest / 4


def build_pdf(
    days: list,
    *,
    who: str,
    target,
    generated_at: datetime,
    start: date | None = None,
    end: date | None = None,
) -> bytes:
    """`days` is a list of (day, meals, totals) for the days with meals;
    `start`/`end` is the period asked for, so the charts show the empty days too."""
    fonts = _fonts()
    buffer = BytesIO()
    pdf = Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Eating diary — {who}")
    pdf.setAuthor("Nutrijurnal")

    first = start or (days[0][0] if days else generated_at.date())
    last = end or (days[-1][0] if days else first)
    period = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    by_day = {day: totals for day, _, totals in days}
    page = 0

    def text(x, y, value, *, size=9.0, font="regular", color=INK, align="left"):
        pdf.setFillColor(color)
        pdf.setFont(fonts[font], size)
        if align == "right":
            pdf.drawRightString(x, y, value)
        elif align == "centre":
            pdf.drawCentredString(x, y, value)
        else:
            pdf.drawString(x, y, value)

    def hairline(x1, y1, x2, y2, colour=RULE, width=0.5):
        pdf.setStrokeColor(colour)
        pdf.setLineWidth(width)
        pdf.line(x1, y1, x2, y2)

    def card(x, y_top, width, height):
        pdf.setFillColor(BAND)
        pdf.roundRect(x, y_top - height, width, height, 10, stroke=0, fill=1)

    def span_label() -> str:
        if first == last:
            return first.strftime("%d %b %Y")
        if first.year == last.year:
            return f"{first:%d %b} – {last:%d %b %Y}"
        return f"{first:%d %b %Y} – {last:%d %b %Y}"

    def brand_row(y: float) -> None:
        if LOGO.exists():
            pdf.drawImage(str(LOGO), MARGIN, y - 4, width=13.2, height=15, mask="auto")
            text(MARGIN + 19, y, "Nutrijurnal", size=10.5, font="semibold", color=ACCENT)
        else:
            text(MARGIN, y, "Nutrijurnal", size=10.5, font="semibold", color=ACCENT)
        text(
            MARGIN + CONTENT_W,
            y,
            f"{who}  ·  {generated_at.strftime('%d %b %Y, %H:%M')}",
            size=7.5,
            color=MUTED,
            align="right",
        )

    def footer() -> None:
        text(MARGIN, MARGIN - 16, f"Eating diary · {span_label()}", size=7, color=FAINT)
        text(MARGIN + CONTENT_W, MARGIN - 16, f"Page {page}", size=7, color=FAINT, align="right")

    def new_page() -> float:
        nonlocal page
        if page:
            footer()
            pdf.showPage()
        page += 1
        y = PAGE_H - MARGIN - 4
        brand_row(y)
        return y - 30

    # --- Page one: the period at a glance -----------------------------------

    y = new_page()
    text(MARGIN, y - 18, "Eating diary", size=26, font="bold")
    logged = len(days)
    text(
        MARGIN,
        y - 38,
        f"{span_label()}  ·  {logged} of {len(period)} {_days(len(period))} logged",
        size=10.5,
        color=MUTED,
    )
    y -= 62

    if not days:
        text(MARGIN, y - 10, "Nothing written down for this period.", size=11, color=MUTED)
        footer()
        pdf.save()
        return buffer.getvalue()

    averages = {
        name: sum(totals[name] for _, _, totals in days) / logged
        for name in ("kcal", "protein", "carbs", "fat")
    }
    goal = {
        name: float(getattr(target, name) or 0) if target is not None else 0.0
        for name in ("kcal", "protein", "carbs", "fat")
    }

    # Four tiles: energy, then each macro against its target
    gap = 8.0
    tile_w = (CONTENT_W - 3 * gap) / 4
    tile_h = 64.0
    tiles = [("kcal", "Energy", "kcal", KCAL)] + [
        (key, label, "g", colour) for key, label, colour, _, _ in MACROS
    ]
    for index, (key, label, unit, colour) in enumerate(tiles):
        x = MARGIN + index * (tile_w + gap)
        card(x, y, tile_w, tile_h)
        pdf.setFillColor(colour)
        pdf.circle(x + 14, y - 15.5, 3, stroke=0, fill=1)
        text(x + 22, y - 18, f"{label} a day".upper(), size=6.5, font="semibold", color=MUTED)
        value = averages[key]
        shown = _thousands(value) if key == "kcal" else f"{value:.0f}"
        over = goal[key] and key == "kcal" and value > goal[key]
        text(x + 12, y - 41, shown, size=19, font="bold", color=OVER_INK if over else INK)
        width = stringWidth(shown, fonts["bold"], 19)
        text(x + 15 + width, y - 41, unit, size=9, color=MUTED)
        if goal[key]:
            delta = value - goal[key]
            note = (
                f"of {_thousands(goal[key])} · {abs(delta):.0f} {'over' if delta > 0 else 'under'}"
            )
            text(x + 12, y - 55, note, size=7, color=MUTED)
        else:
            text(x + 12, y - 55, "average of the days logged", size=7, color=MUTED)
    y -= tile_h + 16

    def axis_labels(x0: float, plot_w: float, y_base: float) -> None:
        slot = plot_w / len(period)
        every = max(1, -(-len(period) // 16))
        for index, day in enumerate(period):
            if index % every and index != len(period) - 1:
                continue
            label = f"{day:%a}"[0] + f" {day.day}" if len(period) <= 14 else str(day.day)
            text(
                x0 + slot * (index + 0.5), y_base - 11, label, size=6.5, color=MUTED, align="centre"
            )

    def bars(x0, y_top, plot_w, plot_h, values, colour_of, reference=None, unit=""):
        """Columns per day from one baseline, a dashed reference across them."""
        highest = max([value for value in values if value] + [reference or 0, 1])
        top, step = _nice_top(highest * 1.08)
        base = y_top - plot_h
        tick = step
        hairline(x0, base, x0 + plot_w, base, FAINT)
        while tick <= top + 0.01:
            y_tick = base + plot_h * tick / top
            hairline(x0, y_tick, x0 + plot_w, y_tick)
            text(x0 - 5, y_tick - 2.5, _thousands(tick), size=6, color=FAINT, align="right")
            tick += step
        slot = plot_w / len(values)
        width = max(1.5, min(18.0, slot * 0.62))
        tops = []
        for index, value in enumerate(values):
            if not value:
                continue
            height = plot_h * value / top
            x = x0 + slot * index + (slot - width) / 2
            pdf.setFillColor(colour_of(value))
            pdf.roundRect(x, base, width, height, min(3.0, width / 2), stroke=0, fill=1)
            tops.append((x + width / 2, base + height, value))
        if reference:
            y_ref = base + plot_h * reference / top
            pdf.setDash(3, 2.5)
            hairline(x0, y_ref, x0 + plot_w, y_ref, INK, 0.7)
            pdf.setDash()
            label = f"Target {_thousands(reference)}{unit}"
            width_label = stringWidth(label, fonts["semibold"], 6.5)
            pdf.setFillColor(BAND)
            pdf.rect(
                x0 + plot_w - width_label - 3, y_ref + 1.5, width_label + 3, 9, stroke=0, fill=1
            )
            text(
                x0 + plot_w, y_ref + 3.5, label, size=6.5, font="semibold", color=INK, align="right"
            )
        if len(values) <= 14:
            for centre, height_top, value in tops:
                shown = _thousands(value)
                width_value = stringWidth(shown, fonts["regular"], 6)
                pdf.setFillColor(BAND)
                pdf.rect(
                    centre - width_value / 2 - 1.5,
                    height_top + 1.5,
                    width_value + 3,
                    8,
                    stroke=0,
                    fill=1,
                )
                text(centre, height_top + 3.5, shown, size=6, color=MUTED, align="centre")
        axis_labels(x0, plot_w, base)

    # Energy, a day at a time
    chart_h = 178.0
    card(MARGIN, y, CONTENT_W, chart_h)
    text(MARGIN + 14, y - 20, "Energy", size=11, font="semibold")
    text(MARGIN + 60, y - 20, "kcal a day", size=8.5, color=MUTED)
    legend_x = MARGIN + CONTENT_W - 14
    for label, colour in (("over target", OVER), ("within target", KCAL)):
        width = stringWidth(label, fonts["regular"], 7)
        text(legend_x, y - 20, label, size=7, color=MUTED, align="right")
        pdf.setFillColor(colour)
        pdf.circle(legend_x - width - 6, y - 17.5, 2.6, stroke=0, fill=1)
        legend_x -= width + 22
    bars(
        MARGIN + 40,
        y - 38,
        CONTENT_W - 56,
        chart_h - 72,
        [by_day[day]["kcal"] if day in by_day else 0 for day in period],
        lambda value: OVER if goal["kcal"] and value > goal["kcal"] else KCAL,
        reference=goal["kcal"] or None,
    )
    y -= chart_h + 12

    # Where the energy came from, and protein a day
    half = (CONTENT_W - gap) / 2
    small_h = 150.0
    card(MARGIN, y, half, small_h)
    text(MARGIN + 14, y - 20, "Where the energy came from", size=11, font="semibold")
    energy = {key: averages[key] * per_gram for key, _, _, _, per_gram in MACROS}
    whole = sum(energy.values()) or 1.0
    bar_x, bar_w, bar_y = MARGIN + 14, half - 28, y - 46
    pdf.saveState()
    clip = pdf.beginPath()
    clip.roundRect(bar_x, bar_y, bar_w, 12, 6)
    pdf.clipPath(clip, stroke=0, fill=0)
    x = bar_x
    for key, _, colour, _, _ in MACROS:
        width = bar_w * energy[key] / whole
        pdf.setFillColor(colour)
        pdf.rect(x, bar_y, width, 12, stroke=0, fill=1)
        x += width
    pdf.restoreState()
    row_y = bar_y - 22
    for key, label, colour, ink, _ in MACROS:
        pdf.setFillColor(colour)
        pdf.circle(bar_x + 3, row_y + 2.5, 3, stroke=0, fill=1)
        text(bar_x + 11, row_y, label, size=8.5, font="semibold")
        text(bar_x + 62, row_y, f"{averages[key]:.0f} g", size=8.5)
        text(
            bar_x + 106,
            row_y,
            f"{energy[key] / whole * 100:.0f}%",
            size=8.5,
            color=ink,
            font="semibold",
        )
        if goal[key]:
            text(
                bar_x + bar_w,
                row_y,
                f"target {goal[key]:.0f} g",
                size=7.5,
                color=MUTED,
                align="right",
            )
        row_y -= 22
    text(
        bar_x, y - small_h + 12, "Average of the days logged, share of kcal", size=6.5, color=FAINT
    )

    protein_x = MARGIN + half + gap
    card(protein_x, y, half, small_h)
    text(protein_x + 14, y - 20, "Protein", size=11, font="semibold")
    text(protein_x + 58, y - 20, "g a day", size=8.5, color=MUTED)
    bars(
        protein_x + 34,
        y - 36,
        half - 48,
        small_h - 66,
        [by_day[day]["protein"] if day in by_day else 0 for day in period],
        lambda value: MACROS[0][2],
        reference=goal["protein"] or None,
        unit=" g",
    )
    y -= small_h + 26

    # --- Day by day ------------------------------------------------------------

    def column_heads(y: float) -> float:
        text(MARGIN, y, "DAY BY DAY", size=7, font="semibold", color=MUTED)
        for x, (_, _, _, ink, _), letter in zip((COL_P, COL_C, COL_F), MACROS, "PCF", strict=True):
            text(MARGIN + x, y, letter, size=7, font="semibold", color=ink, align="right")
        text(MARGIN + COL_KCAL, y, "KCAL", size=7, font="semibold", color=MUTED, align="right")
        return y - 12

    def numbers(
        y, protein, carbs, fat, kcal, *, size=8.5, font="regular", colour=INK, kcal_colour=None
    ):
        for x, value in ((COL_P, protein), (COL_C, carbs), (COL_F, fat)):
            text(MARGIN + x, y, f"{value:.0f}", size=size, font=font, color=colour, align="right")
        text(
            MARGIN + COL_KCAL,
            y,
            _thousands(kcal),
            size=size,
            font=font,
            color=kcal_colour or colour,
            align="right",
        )

    def room(y: float, needed: float) -> float:
        if y - needed < MARGIN + 18:
            y = new_page()
            return column_heads(y) - 4
        return y

    if y - 120 < MARGIN + 18:
        y = new_page()
    y = column_heads(y) - 4

    for day, meals, totals in days:
        rows = sum(1 + (0 if _single_line(meal) else len(meal.items)) for meal in meals)
        y = room(y, min(34 + 13 * rows, 160))
        pdf.setFillColor(BAND)
        pdf.roundRect(MARGIN, y - 8, CONTENT_W, 22, 6, stroke=0, fill=1)
        text(MARGIN + 10, y, day.strftime("%A, %d %B"), size=9.5, font="semibold")
        over = goal["kcal"] and totals["kcal"] > goal["kcal"]
        numbers(
            y,
            totals["protein"],
            totals["carbs"],
            totals["fat"],
            totals["kcal"],
            size=9,
            font="semibold",
            kcal_colour=OVER_INK if over else ACCENT,
        )
        y -= 24

        for index, meal in enumerate(meals):
            y = room(y, 13 + (0 if _single_line(meal) else 11 * len(meal.items)))
            if index:
                hairline(MARGIN + 44, y + 9, MARGIN + CONTENT_W, y + 9)
            when = meal.at.strftime("%H:%M") if meal.at else ""
            text(MARGIN + 10, y, when, size=7.5, color=MUTED)
            text(
                MARGIN + 44,
                y,
                _truncate(meal.title, fonts["semibold"], 8.5, COL_P - 90),
                size=8.5,
                font="semibold",
            )
            numbers(y, meal.protein, meal.carbs, meal.fat, meal.kcal, font="semibold")
            y -= 12
            if _single_line(meal):
                y -= 3
                continue
            for item in meal.items:
                amount = (
                    f"{item.grams:g} {item.unit}"
                    if item.unit in ("g", "ml")
                    else f"{item.quantity:g} × {item.unit} · {item.grams:g} g"
                )
                line = _truncate(f"{item.label}  {amount}", fonts["regular"], 7.5, COL_P - 100)
                text(MARGIN + 54, y, line, size=7.5, color=MUTED)
                numbers(y, item.protein, item.carbs, item.fat, item.kcal, size=7.5, colour=MUTED)
                y -= 10.5
            y -= 4
        y -= 10

    footer()
    pdf.save()
    return buffer.getvalue()


def _days(count: int) -> str:
    return "day" if count == 1 else "days"


def _single_line(meal) -> bool:
    """A meal that is one item under its own name — a dish logged whole — reads
    once, not twice"""
    return (
        len(meal.items) == 1 and meal.items[0].label.strip().lower() == meal.title.strip().lower()
    )


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
