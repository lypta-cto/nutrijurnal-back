"""The printed diary: page one charts the whole period asked for, empty days
included; a dish logged whole reads once; the axes end on round numbers."""

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

from app.services import eating_report


def _item(label, grams, kcal, protein=10.0, carbs=10.0, fat=5.0, unit="g", quantity=None):
    return SimpleNamespace(
        label=label,
        grams=grams,
        unit=unit,
        quantity=quantity or grams,
        kcal=kcal,
        protein=protein,
        carbs=carbs,
        fat=fat,
    )


def _meal(title, *items, at=time(12, 0)):
    return SimpleNamespace(
        title=title,
        at=at,
        items=list(items),
        kcal=sum(i.kcal for i in items),
        protein=sum(i.protein for i in items),
        carbs=sum(i.carbs for i in items),
        fat=sum(i.fat for i in items),
    )


def _day(day, *meals):
    totals = {
        name: sum(getattr(m, name) for m in meals) for name in ("kcal", "protein", "carbs", "fat")
    }
    return (day, list(meals), totals)


TARGET = SimpleNamespace(kcal=2300, protein=175, carbs=195, fat=90)
NOW = datetime(2026, 10, 3, 19, 30, tzinfo=UTC)


def test_the_axis_ends_on_half_a_step_above_the_highest_day():
    assert eating_report._nice_top(2840 * 1.08) == (3500, 1000)
    assert eating_report._nice_top(180 * 1.08) == (200, 50)


def test_a_dish_logged_whole_reads_once():
    whole = _meal("Pljeskavica u lepinji", _item("Pljeskavica u lepinji", 100, 1000))
    plated = _meal("Lunch", _item("Pirinač", 125, 450), _item("Juneći but", 250, 312))
    assert eating_report._single_line(whole)
    assert not eating_report._single_line(plated)


def test_a_month_with_gaps_prints_its_charts_and_every_logged_day():
    start, end = date(2026, 9, 1), date(2026, 10, 3)
    logged = [
        _day(
            start + timedelta(days=offset), _meal("Lunch", _item("Pirinač", 125, 450 + offset * 40))
        )
        for offset in range(20, 33, 2)
    ]

    pdf = eating_report.build_pdf(
        logged, who="Luka", target=TARGET, generated_at=NOW, start=start, end=end
    )

    assert pdf.startswith(b"%PDF")
    assert pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages") >= 1


def test_a_period_with_nothing_logged_says_so_without_charts():
    pdf = eating_report.build_pdf(
        [],
        who="Luka",
        target=None,
        generated_at=NOW,
        start=date(2026, 10, 1),
        end=date(2026, 10, 3),
    )
    assert pdf.startswith(b"%PDF")


def test_no_target_still_prints():
    day = _day(
        date(2026, 10, 3), _meal("Dinner", _item("Jaje", 165, 236, unit="piece", quantity=3))
    )
    pdf = eating_report.build_pdf([day], who="Ana", target=None, generated_at=NOW)
    assert pdf.startswith(b"%PDF")
