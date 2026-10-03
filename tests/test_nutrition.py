"""Reading what people type — Serbian amounts, units, skipped diacritics and
changing word endings — into foods and grams."""

import pytest
from httpx import AsyncClient

from app.services import nutrition
from app.services.nutrition import FoodLike, grams_for, read_amount
from tests.helpers import PREFIX, auth_headers, meal, own_food

# --- The pieces, one at a time -----------------------------------------------


@pytest.mark.parametrize(
    ("written", "plain"),
    [
        ("Kašičica", "kasicica"),
        ("ŠEĆER", "secer"),
        ("Đumbir  i   ĐUMBIR", "djumbir i djumbir"),
        ("  pileći   file ", "pileci file"),
        ("Žumance", "zumance"),
        (None, ""),
    ],
)
def test_text_is_compared_without_case_diacritics_or_extra_spaces(written, plain):
    assert nutrition.normalize(written) == plain


def test_a_foods_search_key_holds_each_name_once():
    assert nutrition.key_of("Šećer", ["šećera", "Secer", "", "kristal šećer"]) == (
        "secer | secera | kristal secer"
    )


@pytest.mark.parametrize(
    ("written", "quantity", "unit", "name"),
    [
        ("50g ovsenih", 50, "g", "ovsenih"),
        ("50 g ovsenih", 50, "g", "ovsenih"),
        ("50 grama ovsenih", 50, "g", "ovsenih"),
        ("31,25 g ovsenih", 31.25, "g", "ovsenih"),
        ("1.5 kg piletine", 1.5, "kg", "piletine"),
        ("0,5 l mleka", 0.5, "l", "mleka"),
        ("200 ml jogurta", 200, "ml", "jogurta"),
        ("1 merica whey", 1, "scoop", "whey"),
        ("2 merice proteina", 2, "scoop", "proteina"),
        ("dve kašike ovsenih", 2, "tbsp", "ovsenih"),
        ("tri kasicice meda", 3, "tsp", "meda"),
        ("jedna šolja mleka", 1, "cup", "mleka"),
        ("2 kriške hleba", 2, "slice", "hleba"),
        ("3 kom jaja", 3, "piece", "jaja"),
        ("šaka badema", 1, "handful", "badema"),
        ("pola banane", 0.5, "piece", "banane"),
        ("½ banane", 0.5, "piece", "banane"),
        ("1/2 banane", 0.5, "piece", "banane"),
        ("2 porcije", 2, "piece", ""),
        ("banana", 1, "piece", "banana"),
        ("2 jaja", 2, "piece", "jaja"),
        # Past a dozen, a bare number is a weight, not a count
        ("150 piletine", 150, "g", "piletine"),
        ("50g od ovsenih", 50, "g", "ovsenih"),
        # A share of fat is part of the name, not a count
        ("3,5% mleko", 1, "piece", "3,5% mleko"),
        ("2 dl 3.2% jogurta", 2, "dl", "3.2% jogurta"),
    ],
)
def test_an_amount_is_read_off_the_front(written, quantity, unit, name):
    amount = read_amount(written)

    assert (amount.quantity, amount.unit, amount.name) == (quantity, unit, name)


def test_a_vague_amount_is_a_small_spoonful():
    amount = read_amount("malo kikiriki putera")

    assert (amount.quantity, amount.unit, amount.name, amount.vague) == (
        0.5,
        "tbsp",
        "kikiriki putera",
        True,
    )


def test_a_pinch_is_a_pinch():
    """ "prstohvat" is a unit, not a vague spoonful: a pinch of cinnamon is
    the half gram the seed says, not a 15 g tablespoon."""
    amount = read_amount("prstohvat cimeta")

    assert (amount.quantity, amount.unit) == (1, "pinch")


def test_grams_come_from_the_foods_own_units_first():
    egg = FoodLike(id=1, name="Jaje", search_key="jaje", units={"piece": 55, "slice": 0})

    assert grams_for(egg, 2, "piece") == 110
    assert grams_for(egg, 2, "g") == 2 and grams_for(egg, 250, "ml") == 250
    assert grams_for(egg, 1.5, "kg") == 1500 and grams_for(egg, 0.33, "l") == 330
    assert grams_for(egg, 2, "dl") == 200
    # A unit the food does not know (or knows as zero) falls back to the default
    assert grams_for(egg, 2, "tbsp") == 30 and grams_for(egg, 1, "slice") == 30
    assert grams_for(None, 1, "cup") == 240
    # A unit nobody has heard of is a 100 g helping rather than nothing
    assert grams_for(None, 2, "serving") == 200


@pytest.mark.parametrize(
    ("text", "chunks"),
    [
        ("50g ovsenih, 1 merica whey, 1 banana", ["50g ovsenih", "1 merica whey", "1 banana"]),
        ("jaje i banana", ["jaje", "banana"]),
        ("egg and banana", ["egg", "banana"]),
        ("2 jaja + 1 banana; kafa\nmed", ["2 jaja", "1 banana", "kafa", "med"]),
        # An "i" inside a word is not a separator
        ("pirinač integralni", ["pirinač integralni"]),
        (" , ; ", []),
    ],
)
def test_a_line_is_split_into_things_eaten(text, chunks):
    assert nutrition.split_text(text) == chunks


@pytest.mark.parametrize(
    ("text", "chunks"),
    [
        ("31,25 g ovsenih", ["31,25 g ovsenih"]),
        ("0,5 l mleka, 1 banana", ["0,5 l mleka", "1 banana"]),
        # Only a comma between two digits is a decimal; one after a word separates
        ("2 jaja,1 banana", ["2 jaja", "1 banana"]),
        ("50 g ovsenih,1,5 kašika meda", ["50 g ovsenih", "1,5 kašika meda"]),
    ],
)
def test_a_decimal_comma_is_not_a_separator(text, chunks):
    assert nutrition.split_text(text) == chunks


def test_totals_add_up_to_a_tenth():
    rows = [{"kcal": 10.04, "protein": 1}, {"kcal": 10.04, "fat": None}, {}]

    assert nutrition.total(rows) == {"kcal": 20.1, "protein": 1.0, "carbs": 0.0, "fat": 0.0}


# --- Through the API, against the foods that actually ship --------------------


@pytest.fixture
async def me(client: AsyncClient, pantry) -> dict:
    return await auth_headers(client, email="parser@example.com")


async def _parse(client: AsyncClient, headers: dict, text: str) -> dict:
    response = await client.post(f"{PREFIX}/eating/parse", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _rows(parsed: dict) -> list[tuple]:
    return [(item["label"], item["unit"], item["grams"]) for item in parsed["items"]]


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("50g ovsenih", [("Ovsene pahuljice", "g", 50)]),
        ("1 merica whey", [("Whey protein", "scoop", 30)]),
        ("2 jaja", [("Jaje", "piece", 110)]),
        ("tri jajeta", [("Jaje", "piece", 165)]),
        ("pola banane", [("Banana", "piece", 60)]),
        ("dve kašike ovsenih", [("Ovsene pahuljice", "tbsp", 20)]),
        ("kasicica meda", [("Med", "tsp", 7)]),
        ("1 čaša mleka", [("Mleko 2.8%", "cup", 250)]),
        ("2 kriške hleba", [("Hleb", "slice", 60)]),
        ("200 ml jogurta", [("Jogurt", "ml", 200)]),
        ("malo putera", [("Puter", "tbsp", 6)]),
        ("pola šake badema", [("Badem", "handful", 15)]),
        ("150 piletine", [("Pileći file", "g", 150)]),
        # A staple nobody counts in pieces: one of it is a helping
        ("1 ovsene pahuljice", [("Ovsene pahuljice", "handful", 30)]),
        # English names reach the shared foods through name_en
        ("50g oats, 1 egg", [("Ovsene pahuljice", "g", 50), ("Jaje", "piece", 55)]),
    ],
)
async def test_serbian_amounts_resolve_against_the_shipped_foods(
    client: AsyncClient, me, text, rows
):
    parsed = await _parse(client, me, text)

    assert parsed["unknown"] == []
    assert _rows(parsed) == rows


async def test_diacritics_and_case_do_not_matter(client: AsyncClient, me):
    for text in ("8 g šećera", "8 g secera", "8 G ŠEĆERA", "8 g Secera"):
        assert _rows(await _parse(client, me, text)) == [("Šećer", "g", 8)], text


async def test_sugar_is_not_sweetcorn_when_spelled_as_sugar(client: AsyncClient, me):
    """ "secer" and "secerac" share a five-letter stem; the spelling the food
    itself uses settles it, whichever way round the words are written."""
    assert _rows(await _parse(client, me, "10 g šećera")) == [("Šećer", "g", 10)]
    assert _rows(await _parse(client, me, "100 g kukuruza šećerca")) == [
        ("Kukuruz šećerac", "g", 100)
    ]
    assert _rows(await _parse(client, me, "100 g šećerca")) == [("Kukuruz šećerac", "g", 100)]


async def test_a_whole_meal_in_one_line_with_its_numbers(client: AsyncClient, me):
    parsed = await _parse(client, me, "50g ovsenih, 1 merica whey, 1 banana i 2 jaja")

    assert [item["label"] for item in parsed["items"]] == [
        "Ovsene pahuljice",
        "Whey protein",
        "Banana",
        "Jaje",
    ]
    # 189.5 + 114 + 106.8 + 170.5
    assert round(sum(item["kcal"] for item in parsed["items"]), 1) == 580.8
    assert all(item["food_id"] for item in parsed["items"])


async def test_what_is_not_recognised_is_handed_back_as_written(client: AsyncClient, me):
    parsed = await _parse(client, me, "100g đumbira, 1 banana, nešto ukusno")

    assert _rows(parsed) == [("Banana", "piece", 120)]
    assert parsed["unknown"] == ["100g đumbira", "nešto ukusno"]


async def test_the_text_to_read_is_bounded(client: AsyncClient, me):
    for text in ("", "x" * 2001):
        response = await client.post(f"{PREFIX}/eating/parse", json={"text": text}, headers=me)
        assert response.status_code == 422


async def test_ones_own_foods_are_read_too_until_archived(client: AsyncClient, me):
    granola = await own_food(
        client, me, name="Bakina granola", kcal=450, units={"cup": 60}, aliases=["granolice"]
    )

    assert _rows(await _parse(client, me, "1 šolja bakine granole")) == [
        ("Bakina granola", "cup", 60)
    ]
    assert _rows(await _parse(client, me, "2 šolje granolice")) == [("Bakina granola", "cup", 120)]

    await client.patch(
        f"{PREFIX}/eating/foods/{granola['id']}", json={"archived": True}, headers=me
    )
    labels = [
        item["label"] for item in (await _parse(client, me, "1 šolja bakine granole"))["items"]
    ]
    assert "Bakina granola" not in labels


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("31,25 g ovsenih", [("Ovsene pahuljice", "g", 31.25)]),
        ("1,5 kašika meda", [("Med", "tbsp", 31.5)]),
        # A litre is handed back as millilitres, the unit the diary keeps
        ("0,5 l mleka", [("Mleko 2.8%", "ml", 500)]),
    ],
)
async def test_a_decimal_comma_reads_as_a_decimal(client: AsyncClient, me, text, rows):
    parsed = await _parse(client, me, text)

    assert parsed["unknown"] == []
    assert _rows(parsed) == rows


async def test_a_parsed_amount_is_saved_as_parsed(client: AsyncClient, me):
    """The parser hands kilos and litres back as grams and millilitres, so
    whatever the page posts of its answer is saved at the same weight."""
    parsed = await _parse(client, me, "1 kg piletine, 0.5 l mleka")
    assert _rows(parsed) == [("Pileći file", "g", 1000), ("Mleko 2.8%", "ml", 500)]
    assert [item["quantity"] for item in parsed["items"]] == [1000, 500]

    saved = await meal(
        client,
        me,
        "2026-09-21",
        *[
            {"food_id": item["food_id"], "quantity": item["quantity"], "unit": item["unit"]}
            for item in parsed["items"]
        ],
    )

    assert [item["grams"] for item in saved["items"]] == [1000, 500]
    assert saved["kcal"] == round(sum(item["kcal"] for item in parsed["items"]), 1)


async def test_one_of_a_drink_is_a_helping_not_a_drop(client: AsyncClient, me):
    for text in ("1 jogurt", "1 pivo"):
        [item] = (await _parse(client, me, text))["items"]
        assert item["grams"] >= 100, text


async def test_a_kilo_or_a_litre_posted_as_such_is_kept_at_its_weight(client: AsyncClient, me):
    """An older page (or any client) may still post the unit it read — the
    diary and the recipe book scale it instead of reading "1 kg" as 1 g."""
    chicken = (await _parse(client, me, "piletina"))["items"][0]["food_id"]

    saved = await meal(
        client, me, "2026-09-21", {"food_id": chicken, "quantity": 1.5, "unit": "kg"}
    )
    [item] = saved["items"]
    assert (item["quantity"], item["unit"], item["grams"]) == (1500, "g", 1500)

    edited = await client.patch(
        f"{PREFIX}/eating/meals/{saved['id']}/items/{item['id']}",
        json={"quantity": 0.25, "unit": "kg"},
        headers=me,
    )
    [item] = edited.json()["items"]
    assert (item["quantity"], item["unit"], item["grams"]) == (250, "g", 250)

    recipe = await client.post(
        f"{PREFIX}/eating/recipes",
        json={"title": "Supa", "items": [{"label": "Voda", "quantity": 1, "unit": "l"}]},
        headers=me,
    )
    [line] = recipe.json()["items"]
    assert (line["quantity"], line["unit"], line["grams"]) == (1000, "ml", 1000)


async def test_a_paste_of_more_than_fifty_things_reads_the_first_fifty(client: AsyncClient, me):
    """Every piece is matched against every food a person can see, so a runaway
    paste is capped — the rest comes back as written, nothing is lost."""
    parsed = await _parse(client, me, ", ".join(["1 banana"] * 60))

    assert len(parsed["items"]) == 50
    assert parsed["unknown"] == ["1 banana"] * 10


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        # Glued to the number, with a point or a comma, or as a word
        ("1,5kg piletine", [("Pileći file", "g", 1500)]),
        ("0.5l mleka", [("Mleko 2.8%", "ml", 500)]),
        ("kilo piletine", [("Pileći file", "g", 1000)]),
        # A share of a litre, written as a fraction or said
        ("1/2 l mleka", [("Mleko 2.8%", "ml", 500)]),
        ("pola litre mleka", [("Mleko 2.8%", "ml", 500)]),
        ("1,5 litara vode", [("Voda", "ml", 1500)]),
        ("prstohvat soli", [("So", "pinch", 0.5)]),
    ],
)
async def test_kilos_litres_and_pinches_come_back_at_their_weight(
    client: AsyncClient, me, text, rows
):
    parsed = await _parse(client, me, text)

    assert parsed["unknown"] == []
    assert _rows(parsed) == rows


async def test_a_unit_changed_on_its_own_keeps_the_amount_it_was(client: AsyncClient, me):
    """Changing only the unit of a line keeps the quantity typed beside it:
    200 g made into millilitres is 200 ml, not 200 of something else."""
    yogurt = (await _parse(client, me, "jogurt"))["items"][0]["food_id"]
    saved = await meal(client, me, "2026-09-21", {"food_id": yogurt, "quantity": 200, "unit": "g"})
    [item] = saved["items"]

    edited = await client.patch(
        f"{PREFIX}/eating/meals/{saved['id']}/items/{item['id']}",
        json={"unit": "ml"},
        headers=me,
    )

    [item] = edited.json()["items"]
    assert (item["quantity"], item["unit"], item["grams"]) == (200, "ml", 200)


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        # How a glass of milk, juice or yogurt is said in Serbian
        ("2 dl mleka", [("Mleko 2.8%", "ml", 200)]),
        ("1 dl jogurta", [("Jogurt", "ml", 100)]),
        ("3 decilitra mleka", [("Mleko 2.8%", "ml", 300)]),
        ("2dl mleka", [("Mleko 2.8%", "ml", 200)]),
        ("1,5 dcl jogurta", [("Jogurt", "ml", 150)]),
    ],
)
async def test_a_decilitre_is_a_tenth_of_a_litre(client: AsyncClient, me, text, rows):
    parsed = await _parse(client, me, text)

    assert parsed["unknown"] == []
    assert _rows(parsed) == rows
    # Handed back in millilitres, the unit the diary keeps
    assert [item["quantity"] for item in parsed["items"]] == [row[2] for row in rows]


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        # The endings Serbian puts on them after a number or a verb
        ("2 litra vode", [("Voda", "ml", 2000)]),
        ("litru mleka", [("Mleko 2.8%", "ml", 1000)]),
        ("pola kile piletine", [("Pileći file", "g", 500)]),
        ("1 kila jabuka", [("Jabuka", "g", 1000)]),
        ("dva kilograma krompira", [("Krompir", "g", 2000)]),
        ("250 mililitara mleka", [("Mleko 2.8%", "ml", 250)]),
    ],
)
async def test_litres_and_kilos_with_any_case_ending(client: AsyncClient, me, text, rows):
    parsed = await _parse(client, me, text)

    assert parsed["unknown"] == []
    assert _rows(parsed) == rows


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG app/services/nutrition.py read_amount() only reads an amount off the FRONT of a "
        "line; a list typed as 'piletina 200 g' or 'chicken 250g' — the way a label or a "
        "plan writes it — is one piece of the food (150 g of chicken, a 250 ml glass of "
        "yogurt) and the number written is ignored"
    ),
)
@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("piletina 200 g", [("Pileći file", "g", 200)]),
        ("jogurt 200 ml", [("Jogurt", "ml", 200)]),
        ("chicken 250g", [("Pileći file", "g", 250)]),
    ],
)
async def test_an_amount_written_after_the_food(client: AsyncClient, me, text, rows):
    assert _rows(await _parse(client, me, text)) == rows


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG app/services/nutrition.py read_amount(): after 'half' the 'a' in front of the "
        "unit is taken for part of the name, so the unit is never read — 'half a litre of "
        "milk' is half a glass (125 ml) and 'half a kilo of chicken' half a piece (75 g)"
    ),
)
@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("half a litre of milk", [("Mleko 2.8%", "ml", 500)]),
        ("half a kilo of chicken", [("Pileći file", "g", 500)]),
    ],
)
async def test_half_a_unit_said_in_english(client: AsyncClient, me, text, rows):
    assert _rows(await _parse(client, me, text)) == rows
