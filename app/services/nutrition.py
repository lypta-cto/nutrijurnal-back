"""
Turning what a person types into grams, and grams into macros.

Two jobs. `resolve` reads one written amount — "50g ovsenih", "1 merica
whey", "pola banane", "malo kikiriki putera" — into a food, a quantity and
a unit, and then into grams. `macros` turns grams into kcal and the three
macros using the food's per-100 g numbers. Serbian is written with
diacritics people skip and nouns that change ending with the number, so
matching is done on stripped, prefix-trimmed words rather than exact names.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Grams for a named unit when the food itself doesn't say. Deliberately
# modest: an unweighed spoon is the place to be conservative.
DEFAULT_UNIT_GRAMS: dict[str, float] = {
    "g": 1.0,
    "ml": 1.0,
    "piece": 100.0,
    "scoop": 30.0,
    "tbsp": 15.0,
    "tsp": 5.0,
    "cup": 240.0,
    "handful": 30.0,
    "pinch": 1.0,
    "slice": 30.0,
}

# How the same unit is written in Serbian (and how people actually type it)
UNIT_WORDS: dict[str, str] = {
    "g": "g",
    "gr": "g",
    "gram": "g",
    "grama": "g",
    "grami": "g",
    "g.": "g",
    "kg": "kg",
    # A kilo and a litre change their ending with the number and the verb:
    # "2 litra vode", "popio sam litru", "pola kile", "1 kila jabuka"
    "kila": "kg",
    "kile": "kg",
    "kilu": "kg",
    "kilom": "kg",
    "kilograma": "kg",
    "kilogramu": "kg",
    "ml": "ml",
    "mililitar": "ml",
    "mililitra": "ml",
    "mililitara": "ml",
    "mil": "ml",
    "l": "l",
    "litar": "l",
    "litra": "l",
    "litru": "l",
    "litri": "l",
    "litrom": "l",
    "litara": "l",
    # A glass of milk, juice or yogurt is said in decilitres
    "dl": "dl",
    "dcl": "dl",
    "decilitar": "dl",
    "decilitra": "dl",
    "decilitre": "dl",
    "decilitara": "dl",
    "kom": "piece",
    "komad": "piece",
    "komada": "piece",
    "kome": "piece",
    "merica": "scoop",
    "merice": "scoop",
    "merici": "scoop",
    "mericu": "scoop",
    "scoop": "scoop",
    "skup": "scoop",
    "kasika": "tbsp",
    "kasike": "tbsp",
    "kasiku": "tbsp",
    "kasicu": "tbsp",
    "supena": "tbsp",
    "kasicica": "tsp",
    "kasicice": "tsp",
    "kasicicu": "tsp",
    "saka": "handful",
    "sake": "handful",
    "saku": "handful",
    "sacica": "handful",
    "prstohvat": "pinch",
    "prstohvata": "pinch",
    "kriska": "slice",
    "kriske": "slice",
    "krisku": "slice",
    "parce": "slice",
    "solja": "cup",
    "solje": "cup",
    "solju": "cup",
    "casa": "cup",
    "porcija": "serving",
    "porcije": "serving",
    "porciju": "serving",
    # The same, said in English
    "grams": "g",
    "kilo": "kg",
    "kilos": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "millilitres": "ml",
    "milliliters": "ml",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",
    "decilitres": "dl",
    "deciliter": "dl",
    "deciliters": "dl",
    "piece": "piece",
    "pieces": "piece",
    "pcs": "piece",
    "scoops": "scoop",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "tbsp": "tbsp",
    "spoon": "tbsp",
    "spoons": "tbsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "tsp": "tsp",
    "cup": "cup",
    "cups": "cup",
    "glass": "cup",
    "glasses": "cup",
    "handful": "handful",
    "handfuls": "handful",
    "pinch": "pinch",
    "slice": "slice",
    "slices": "slice",
    "serving": "serving",
    "servings": "serving",
    "portion": "serving",
    "portions": "serving",
}

# Numbers said in words, the way dictation and a quick thumb write them.
# Serbian builds a number by adding its words ("dvesta pedeset" is 250), and
# a hundred said after a digit word multiplies it ("pet sto", "two hundred")
CARDINALS: dict[str, float] = {
    "jedan": 1,
    "jedna": 1,
    "jedno": 1,
    "jednu": 1,
    "dva": 2,
    "dve": 2,
    "dvije": 2,
    "tri": 3,
    "cetiri": 4,
    "pet": 5,
    "sest": 6,
    "sedam": 7,
    "osam": 8,
    "devet": 9,
    "deset": 10,
    "jedanaest": 11,
    "dvanaest": 12,
    "trinaest": 13,
    "cetrnaest": 14,
    "petnaest": 15,
    "sesnaest": 16,
    "sedamnaest": 17,
    "osamnaest": 18,
    "devetnaest": 19,
    "dvadeset": 20,
    "trideset": 30,
    "cetrdeset": 40,
    "pedeset": 50,
    "sezdeset": 60,
    "sedamdeset": 70,
    "osamdeset": 80,
    "devedeset": 90,
    "sto": 100,
    "stotinu": 100,
    "dvesta": 200,
    "dvjesto": 200,
    "trista": 300,
    "tristo": 300,
    "cetiristo": 400,
    "petsto": 500,
    "sesto": 600,
    "seststo": 600,
    "sedamsto": 700,
    "osamsto": 800,
    "devetsto": 900,
    "hiljadu": 1000,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}
# Words that stand for an amount on their own, never added to another
WORD_QUANTITY: dict[str, float] = {
    "pola": 0.5,
    "polovina": 0.5,
    "polovinu": 0.5,
    "pol": 0.5,
    "cetvrt": 0.25,
    "cetvrtina": 0.25,
    "cetvrtinu": 0.25,
    "par": 2,
    "nekoliko": 3,
    "half": 0.5,
    "quarter": 0.25,
    "a": 1,
    "an": 1,
    "couple": 2,
    "few": 3,
}
_SAID_WITH_AN_ARTICLE = (CARDINALS.keys() | WORD_QUANTITY.keys()) - {"a", "an"}
# Vague amounts: a small helping of whatever it is
VAGUE = {
    "malo": 0.5,
    "kap": 0.25,
    "po zelji": 0.5,
    "po ukusu": 0.5,
    "a little": 0.5,
    "a bit of": 0.5,
    "a splash of": 0.25,
    "a drop of": 0.25,
}

FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}

# A comma between two digits is the Serbian decimal comma ("0,5 l mleka",
# "31,25 g") and stays inside its amount; every other comma separates foods
SPLIT = re.compile(r"[\n;+]|(?<!\d),|,(?!\d)|(?:\s+\bi\b\s+)|(?:\s+\band\b\s+)")
# A number followed by "%" is part of the name ("3,5% mleko"), not an amount
NUMBER = re.compile(r"^\s*(\d+(?:[.,]\d+)?)(?![\d.,]*\s*%)\s*(?:/\s*(\d+))?")
# An amount after the food, the way a label or a plan lists it: "piletina
# 200 g", "chicken 250g". Only with its unit — a bare number at the end is as
# often part of the name ("Mleko 1.6") as an amount
TRAILING = re.compile(r"\s(\d+(?:[.,]\d+)?)\s*(?:/\s*(\d+))?\s*([a-z]+)$")


def strip_accents(text: str) -> str:
    """ "Kašičica" and "kasicica" are the same word to anyone typing fast."""
    text = text.replace("đ", "dj").replace("Đ", "DJ")
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", strip_accents(text or "").lower()).strip()


def stem(word: str) -> str:
    """Serbian endings change with the number and the case; the first five
    letters do not ("ovsene", "ovsenih", "ovsenim")."""
    return word[:5]


def key_of(name: str, aliases: list[str] | None = None) -> str:
    """The searchable text stored on a food: its names and every alias."""
    parts = [normalize(name)] + [normalize(alias) for alias in (aliases or [])]
    return " | ".join(dict.fromkeys(part for part in parts if part))


@dataclass
class FoodLike:
    """What `resolve` needs of a food — the ORM row or a plain seed dict."""

    id: object
    name: str
    search_key: str
    units: dict | None
    base_unit: str = "g"
    kcal: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0


# A kilo, a litre and a decilitre are only other spellings of the base units.
# The diary keeps amounts in the units every picker knows, so "1 kg piletine"
# is written down as 1000 g — never as 1 of a unit the diary would read as a gram.
SCALED_UNITS: dict[str, tuple[str, float]] = {
    "kg": ("g", 1000),
    "l": ("ml", 1000),
    "dl": ("ml", 100),
}


def in_base_units(quantity: float, unit: str) -> tuple[float, str]:
    """ "1.5 kg" → 1500 g, "0.5 l" → 500 ml; every other amount as it was."""
    if unit in SCALED_UNITS:
        base, factor = SCALED_UNITS[unit]
        return round(quantity * factor, 2), base
    return quantity, unit


def grams_for(food: FoodLike | None, quantity: float, unit: str) -> float:
    """How many grams (or millilitres) an amount comes to."""
    unit = unit or "g"
    if unit in ("g", "ml"):
        return round(quantity, 2)
    if unit in SCALED_UNITS:
        return round(quantity * SCALED_UNITS[unit][1], 2)
    per_unit = None
    if food is not None and isinstance(food.units, dict):
        value = food.units.get(unit)
        if isinstance(value, int | float) and value > 0:
            per_unit = float(value)
    if per_unit is None:
        per_unit = DEFAULT_UNIT_GRAMS.get(unit, 100.0)
    return round(quantity * per_unit, 2)


def macros(food: FoodLike | None, grams: float) -> dict[str, float]:
    if food is None:
        return {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
    share = (grams or 0) / 100
    return {
        "kcal": round(food.kcal * share, 1),
        "protein": round(food.protein * share, 1),
        "carbs": round(food.carbs * share, 1),
        "fat": round(food.fat * share, 1),
    }


def total(items: list[dict[str, float]]) -> dict[str, float]:
    out = {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
    for item in items:
        for name in out:
            out[name] += float(item.get(name) or 0)
    return {name: round(value, 1) for name, value in out.items()}


# --- Reading a written amount ------------------------------------------------


@dataclass
class Amount:
    quantity: float
    unit: str
    # What is left after the number and the unit were taken off the front
    name: str
    vague: bool = False


def _number(written: str, divisor: str | None = None) -> float:
    """ "31,25" → 31.25 · "1/2" → 0.5 (a zero under the line is ignored)."""
    quantity = float(written.replace(",", "."))
    if divisor and float(divisor):
        quantity /= float(divisor)
    return quantity


def _said_amount(rest: str) -> tuple[float | None, str]:
    """ "dvesta pedeset grama …" → 250 · "a quarter of a litre …" → 0.25 ·
    "pola banane" → 0.5 — and what is left after the words taken."""
    words = rest.split(" ")
    # "a hundred", "a couple", "a quarter": the article belongs to the amount
    if words[0] in ("a", "an") and len(words) > 1 and words[1] in _SAID_WITH_AN_ARTICLE:
        words = words[1:]
    total, taken = 0.0, 0
    for word in words:
        value = CARDINALS.get(word)
        if value is None:
            break
        total = total * value if value == 100 and 0 < total < 10 else total + value
        taken += 1
    if taken:
        return total, " ".join(words[taken:])
    if words[0] in WORD_QUANTITY:
        return WORD_QUANTITY[words[0]], " ".join(words[1:])
    return None, rest


def read_amount(text: str) -> Amount:
    """ "50g ovsenih" → 50 g ovsenih · "1 merica whey" → 1 scoop whey ·
    "malo putera" → half a spoon of it · "banana" → one of them ·
    "piletina 200 g" → 200 g piletina."""
    rest = normalize(text).strip(" .-")
    if not rest:
        return Amount(0, "g", "")

    for word, share in VAGUE.items():
        if rest.startswith(word + " "):
            rest = rest[len(word) :].strip()
            return Amount(share, "tbsp", rest, vague=True)

    quantity: float | None = None
    for symbol, value in FRACTIONS.items():
        if rest.startswith(symbol):
            quantity, rest = value, rest[len(symbol) :].strip()
            break
    if quantity is None:
        match = NUMBER.match(rest)
        if match:
            quantity = _number(match.group(1), match.group(2))
            rest = rest[match.end() :].strip()
    if quantity is None and rest:
        quantity, rest = _said_amount(rest)

    unit = ""
    if rest and quantity is not None:
        # "half a litre of milk": the little words English puts between an
        # amount and its unit, taken out only when a unit really follows
        bridged = re.sub(r"^(?:(?:of|an?)\s+)+", "", rest)
        if bridged.split(" ")[0].strip(".") in UNIT_WORDS:
            rest = bridged
    if rest:
        head = rest.split(" ")[0].strip(".")
        mapped = UNIT_WORDS.get(head)
        if mapped:
            unit = mapped
            rest = rest[len(rest.split(" ")[0]) :].strip()
        else:
            # "50g" written without a space
            glued = re.match(r"^(g|gr|ml|kg|dl|l)\b", head)
            if glued and quantity is not None:
                unit = UNIT_WORDS.get(glued.group(1), "g")
                rest = rest[len(glued.group(1)) :].strip()
    if quantity is None and not unit:
        trailing = TRAILING.search(rest)
        if trailing and trailing.group(3) in UNIT_WORDS:
            quantity = _number(trailing.group(1), trailing.group(2))
            unit = UNIT_WORDS[trailing.group(3)]
            rest = rest[: trailing.start()].strip()
    if quantity is None:
        quantity = 1.0
    if not unit:
        unit = "piece" if quantity and quantity <= 12 else "g"
    rest = re.sub(r"^(?:(?:od|sa|of|a|an|the)\s+)+", "", rest).strip(" ().")
    return Amount(quantity, unit, rest)


def match_food(name: str, foods: list[FoodLike]) -> FoodLike | None:
    """The food whose name or aliases best cover the words written. Scored
    on five-letter stems, so "ovsenih pahuljica" finds "ovsene pahuljice"."""
    written = [word for word in re.findall(r"[a-z0-9]+", normalize(name)) if len(word) > 2]
    wanted = [stem(word) for word in written]
    if not wanted:
        return None
    spelled = set(written)
    best, best_score = None, 0.0
    for food in foods:
        for alias in food.search_key.split("|"):
            words = [word for word in re.findall(r"[a-z0-9]+", alias) if len(word) > 2]
            tokens = [stem(word) for word in words]
            if not tokens:
                continue
            hits = sum(1 for token in tokens if token in wanted)
            if not hits:
                continue
            # A word spelled the way the food spells it beats one that only
            # shares a five-letter stem — "secer" is sugar, "secerac" is the
            # sweetcorn that otherwise ties with it and wins on table order
            same = sum(1 for word in words if word in spelled)
            # Every word of the alias matched is worth more than a long
            # alias that only brushed the text
            score = hits / len(tokens) + hits / max(len(wanted), 1) + 0.01 * hits + 0.02 * same
            if score > best_score:
                best, best_score = food, score
    return best if best_score >= 1.0 else None


@dataclass
class Resolved:
    label: str
    quantity: float
    unit: str
    grams: float
    food: FoodLike | None
    raw: str

    @property
    def known(self) -> bool:
        return self.food is not None


# What "a portion" of a staple comes to: the 100 g helping an unknown unit is
# everywhere else (grams_for), not the 30 g handful an uncounted "1 oats" is
PORTION_GRAMS = 100.0


def resolve(text: str, foods: list[FoodLike]) -> Resolved:
    """One written line into an amount of a known food, where we can."""
    amount = read_amount(text)
    food = match_food(amount.name or text, foods)
    quantity, unit = amount.quantity, amount.unit
    known_units = food.units if food is not None and isinstance(food.units, dict) else {}
    if unit == "serving":
        # A portion of rice or pasta is a plateful; of anything counted in
        # pieces, or drunk, it is one of them like any other count
        if food is not None and food.base_unit == "g" and "piece" not in known_units:
            quantity, unit = round(quantity * PORTION_GRAMS, 2), "g"
        else:
            unit = "piece"
    if food is not None and unit == "piece" and "piece" not in known_units:
        # Nobody eats "one oats" — an uncounted staple means a helping,
        # and one of a drink ("1 jogurt", "1 pivo") is a glass of it
        unit = "handful" if food.base_unit == "g" else "cup"
    quantity, unit = in_base_units(quantity, unit)
    grams = grams_for(food, quantity, unit)
    label = food.name if food is not None else (amount.name or text.strip())
    return Resolved(
        label=label,
        quantity=quantity,
        unit=unit,
        grams=grams,
        food=food,
        raw=text.strip(),
    )


def split_text(text: str) -> list[str]:
    """ "50g ovsenih, 1 merica whey i banana" → three things to look up."""
    return [chunk.strip(" .-") for chunk in SPLIT.split(text or "") if chunk and chunk.strip(" .-")]


# Every chunk is matched against every food a person can see; a meal of more
# than this is a paste gone wrong, and the rest is handed back as written
MAX_CHUNKS = 50


def parse(text: str, foods: list[FoodLike]) -> tuple[list[Resolved], list[str]]:
    found, unknown = [], []
    chunks = split_text(text)
    unknown.extend(chunks[MAX_CHUNKS:])
    for chunk in chunks[:MAX_CHUNKS]:
        item = resolve(chunk, foods)
        if item.known:
            found.append(item)
        else:
            unknown.append(chunk)
    return found, unknown
