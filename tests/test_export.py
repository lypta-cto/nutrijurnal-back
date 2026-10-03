"""The diary for a period, as a printable PDF or a spreadsheet-ready CSV."""

import csv
import io
import re
from datetime import date, timedelta

import pytest
from httpx import AsyncClient

from app.services import eating_report
from tests.helpers import PREFIX, auth_headers, find_food, meal

FIRST, LAST = "2026-09-21", "2026-09-23"


@pytest.fixture
async def me(client: AsyncClient, seeds) -> dict:
    return await auth_headers(client, email="export@example.com", full_name="Đorđe Šušić")


@pytest.fixture
async def diary(client: AsyncClient, me) -> None:
    """Two days with meals and an empty one between them."""
    egg = await find_food(client, me, "jaje", "Jaje")
    oats = await find_food(client, me, "ovsene", "Ovsene pahuljice")
    await meal(
        client,
        me,
        FIRST,
        {"food_id": oats["id"], "quantity": 50, "unit": "g"},
        {"food_id": egg["id"], "quantity": 2, "unit": "piece"},
        title="Doručak",
        at="08:30:00",
    )
    await meal(
        client,
        me,
        FIRST,
        {"label": "Kolač", "quantity": 1.5, "macros": {"kcal": 300, "carbs": 40}},
        title="Užina",
    )
    await meal(
        client, me, LAST, {"food_id": egg["id"], "quantity": 1, "unit": "piece"}, title="Večera"
    )


def _export(client: AsyncClient, headers: dict, start: str, end: str, **params):
    return client.get(
        f"{PREFIX}/eating/export", params={"from": start, "to": end, **params}, headers=headers
    )


async def test_the_csv_has_a_row_per_item_and_a_total_per_day(client: AsyncClient, me, diary):
    response = await _export(client, me, FIRST, LAST, format="csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="Nutrijurnal_2026-09-21_2026-09-23.csv"'
    )
    # A byte-order mark, so Excel opens č, ć, š, ž and đ as themselves
    assert response.content.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))

    assert rows == [
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
        ],
        [
            FIRST,
            "08:30",
            "Doručak",
            "Ovsene pahuljice",
            "50",
            "g",
            "50",
            "6.6",
            "33.9",
            "3.2",
            "190",
        ],
        [FIRST, "08:30", "Doručak", "Jaje", "2", "piece", "110", "14.3", "1.2", "12.1", "170"],
        [FIRST, "", "Užina", "Kolač", "1.5", "serving", "150", "0.0", "60.0", "0.0", "450"],
        [FIRST, "", "DAY TOTAL", "", "", "", "", "20.9", "95.1", "15.3", "810"],
        [LAST, "", "Večera", "Jaje", "1", "piece", "55", "7.2", "0.6", "6.1", "85"],
        [LAST, "", "DAY TOTAL", "", "", "", "", "7.2", "0.6", "6.1", "85"],
    ]


async def test_the_pdf_is_a_pdf_named_for_its_period(client: AsyncClient, me, diary):
    response = await _export(client, me, FIRST, LAST, format="pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="Nutrijurnal_2026-09-21_2026-09-23.pdf"'
    )
    assert response.content.startswith(b"%PDF-")
    assert response.content.rstrip().endswith(b"%%EOF")
    # Set in the vendored IBM Plex, which carries the Serbian letters
    assert b"IBMPlexSans" in response.content


async def test_pdf_is_the_default_format(client: AsyncClient, me, diary):
    response = await _export(client, me, FIRST, LAST)

    assert response.content.startswith(b"%PDF-")


async def test_an_empty_period_still_prints(client: AsyncClient, me):
    pdf = await _export(client, me, "2020-01-01", "2020-01-31")
    csv_file = await _export(client, me, "2020-01-01", "2020-01-31", format="csv")

    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert csv_file.content.decode("utf-8-sig").splitlines() == [
        "Day;Time;Meal;Item;Quantity;Unit;Grams;Protein;Carbs;Fat;Kcal"
    ]


async def test_a_long_period_runs_onto_more_pages(client: AsyncClient, me):
    egg = await find_food(client, me, "jaje", "Jaje")
    start = date(2026, 6, 1)
    for offset in range(30):
        day = (start + timedelta(days=offset)).isoformat()
        for title in ("Doručak", "Ručak", "Večera"):
            await meal(
                client,
                me,
                day,
                *[{"food_id": egg["id"], "quantity": 1, "unit": "piece"}] * 3,
                title=title,
            )

    response = await _export(client, me, "2026-06-01", "2026-06-30")

    assert response.status_code == 200
    pages = re.findall(rb"/Type /Page[^s]", response.content)
    assert len(pages) > 5


async def test_the_pdf_is_given_the_persons_name_target_and_days(
    client: AsyncClient, me, diary, monkeypatch
):
    printed = []
    real_build_pdf = eating_report.build_pdf

    def spy(days, **options):
        printed.append((days, options))
        return real_build_pdf(days, **options)

    monkeypatch.setattr(eating_report, "build_pdf", spy)
    await client.patch(f"{PREFIX}/eating/settings", json={"target_kcal": 2300}, headers=me)

    await _export(client, me, FIRST, LAST)

    [(days, options)] = printed
    assert options["who"] == "Đorđe Šušić"
    assert options["target"].kcal == 2300
    # Only the days something was eaten, each with its meals and its total
    assert [
        (str(day), [m.title for m in meals], totals["kcal"]) for day, meals, totals in days
    ] == [
        (FIRST, ["Doručak", "Užina"], 810.0),
        (LAST, ["Večera"], 85.2),
    ]


async def test_the_period_must_run_forwards_and_stay_under_400_days(client: AsyncClient, me):
    backwards = await _export(client, me, LAST, FIRST)
    assert backwards.status_code == 400 and backwards.json()["detail"] == "from is after to"

    too_long = await _export(client, me, "2025-08-16", "2026-09-21", format="csv")
    assert too_long.status_code == 400 and too_long.json()["detail"] == "range is too long"
    assert (await _export(client, me, "2025-08-17", "2026-09-21", format="csv")).status_code == 200

    assert (await _export(client, me, FIRST, LAST, format="xlsx")).status_code == 422
    assert (
        await client.get(f"{PREFIX}/eating/export", params={"from": FIRST}, headers=me)
    ).status_code == 422
    assert (
        await client.get(f"{PREFIX}/eating/export", params={"from": FIRST, "to": LAST})
    ).status_code == 401
