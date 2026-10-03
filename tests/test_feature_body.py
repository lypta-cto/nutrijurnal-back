"""Water by the glass and weight by the day — each person's own, read beside
the food on the day it belongs to."""

from httpx import AsyncClient

from tests.helpers import PREFIX, auth_headers

DAY = "2026-09-21"


async def test_water_adds_up_by_the_glass_and_the_last_one_can_be_taken_back(
    client: AsyncClient,
):
    headers = await auth_headers(client, "water@example.com")

    first = await client.post(
        f"{PREFIX}/eating/water", json={"day": DAY, "ml": 250}, headers=headers
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        f"{PREFIX}/eating/water", json={"day": DAY, "ml": 500}, headers=headers
    )
    body = second.json()
    assert body["ml"] == 750
    # The everyday defaults until the person sets their own
    assert (body["goal_ml"], body["glass_ml"]) == (2000, 250)
    assert [entry["ml"] for entry in body["entries"]] == [250, 500]

    undone = await client.delete(
        f"{PREFIX}/eating/water/{body['entries'][-1]['id']}", headers=headers
    )
    assert undone.json()["ml"] == 250

    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert (day["water_ml"], day["water_goal_ml"]) == (250, 2000)


async def test_water_totals_by_day_for_a_range(client: AsyncClient):
    headers = await auth_headers(client, "range@example.com")
    for day, ml in (("2026-09-20", 300), ("2026-09-21", 250), ("2026-09-21", 250)):
        await client.post(f"{PREFIX}/eating/water", json={"day": day, "ml": ml}, headers=headers)

    response = await client.get(
        f"{PREFIX}/eating/water",
        params={"from": "2026-09-19", "to": "2026-09-22"},
        headers=headers,
    )

    assert response.json() == [{"day": "2026-09-20", "ml": 300}, {"day": "2026-09-21", "ml": 500}]


async def test_nobody_reads_or_drinks_from_someone_elses_glass(client: AsyncClient):
    owner = await auth_headers(client, "glass-owner@example.com")
    stranger = await auth_headers(client, "glass-stranger@example.com")
    added = await client.post(f"{PREFIX}/eating/water", json={"day": DAY, "ml": 250}, headers=owner)
    entry = added.json()["entries"][0]["id"]

    assert (
        await client.delete(f"{PREFIX}/eating/water/{entry}", headers=stranger)
    ).status_code == 404
    theirs = await client.get(f"{PREFIX}/eating/water/{DAY}", headers=stranger)
    assert theirs.json()["ml"] == 0
    assert (await client.get(f"{PREFIX}/eating/water/{DAY}", headers=owner)).json()["ml"] == 250


async def test_silly_amounts_of_water_are_refused(client: AsyncClient):
    headers = await auth_headers(client, "silly@example.com")

    for ml in (0, -250, 50_000):
        response = await client.post(
            f"{PREFIX}/eating/water", json={"day": DAY, "ml": ml}, headers=headers
        )
        assert response.status_code == 422, ml


async def test_a_water_goal_and_glass_of_ones_own(client: AsyncClient):
    headers = await auth_headers(client, "goal@example.com")

    saved = await client.patch(
        f"{PREFIX}/eating/settings",
        json={"water_goal_ml": 2500, "water_glass_ml": 330},
        headers=headers,
    )

    assert (saved.json()["water_goal_ml"], saved.json()["water_glass_ml"]) == (2500, 330)
    day = (await client.get(f"{PREFIX}/eating/water/{DAY}", headers=headers)).json()
    assert (day["goal_ml"], day["glass_ml"]) == (2500, 330)


async def test_one_weight_a_day_and_the_newest_feeds_the_calculator(client: AsyncClient):
    headers = await auth_headers(client, "scale@example.com")
    profile = {"sex": "female", "birth_year": 1990, "height_cm": 168, "weight_kg": 70}
    await client.patch(f"{PREFIX}/eating/settings", json={"profile": profile}, headers=headers)

    await client.put(f"{PREFIX}/eating/weight/2026-09-20", json={"kg": 69.8}, headers=headers)
    first = await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 69.5}, headers=headers)
    # Weighed again the same morning: the number is corrected, not doubled
    again = await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 69.4}, headers=headers)
    # An older day filled in later does not overwrite today's weight
    await client.put(f"{PREFIX}/eating/weight/2026-09-15", json={"kg": 71}, headers=headers)

    assert first.status_code == 200 and again.json() == {"day": DAY, "kg": 69.4}
    listed = await client.get(
        f"{PREFIX}/eating/weight", params={"from": "2026-09-01", "to": DAY}, headers=headers
    )
    assert [entry["kg"] for entry in listed.json()] == [71, 69.8, 69.4]
    latest = await client.get(f"{PREFIX}/eating/weight/latest", headers=headers)
    assert latest.json() == {"day": DAY, "kg": 69.4}
    settings = (await client.get(f"{PREFIX}/eating/settings", headers=headers)).json()
    assert settings["profile"]["weight_kg"] == 69.4
    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=headers)).json()
    assert day["weight_kg"] == 69.4

    removed = await client.delete(f"{PREFIX}/eating/weight/{DAY}", headers=headers)
    assert removed.status_code == 204
    assert (
        await client.delete(f"{PREFIX}/eating/weight/{DAY}", headers=headers)
    ).status_code == 404


async def test_weights_stay_private(client: AsyncClient):
    owner = await auth_headers(client, "weight-owner@example.com")
    stranger = await auth_headers(client, "weight-stranger@example.com")
    await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 80}, headers=owner)

    latest = await client.get(f"{PREFIX}/eating/weight/latest", headers=stranger)
    assert latest.json() is None
    assert (
        await client.delete(f"{PREFIX}/eating/weight/{DAY}", headers=stranger)
    ).status_code == 404
    gone = await client.put(f"{PREFIX}/eating/weight/{DAY}", json={"kg": 5}, headers=owner)
    assert gone.status_code == 422
