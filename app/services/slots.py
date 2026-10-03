"""
Where a meal sits in the day: breakfast, lunch, dinner or a snack.

The diary groups by these four, whatever a meal is called — "Ovsena kaša" is
still breakfast. A meal arriving without a slot takes one from its time, or
from a slot word in its title, so nothing lands in the wrong place just
because the page did not say.
"""

from __future__ import annotations

import re
from datetime import time

from app.services.nutrition import normalize

SLOTS = ("breakfast", "lunch", "dinner", "snack")

LABELS = {"breakfast": "Breakfast", "lunch": "Lunch", "dinner": "Dinner", "snack": "Snack"}

# Every way a slot is said, Serbian (diacritics stripped, every case ending
# people use) and English. Matched as whole words on normalised text.
WORDS: dict[str, tuple[str, ...]] = {
    "breakfast": ("dorucak", "dorucku", "doruckom", "doruckovao", "breakfast"),
    "lunch": ("rucak", "rucku", "ruckom", "rucao", "rucala", "lunch"),
    "dinner": ("vecera", "veceru", "veceri", "vecerom", "vecerao", "dinner", "supper"),
    "snack": ("uzina", "uzinu", "uzini", "snack", "snacks", "uzinica"),
}

_WORD_SLOT = {word: slot for slot, words in WORDS.items() for word in words}


# The slot as it is said in a sentence, with the little word in front of it:
# "za ručak", "na doručku", "for lunch", "at dinner", or "Večera:" as a label.
# Written with both spellings of č/ž, because dictation and typing disagree.
_SLOT_FORMS = {
    "breakfast": r"doru[cč](?:ak|ka|ku|kom)|breakfast",
    "lunch": r"ru[cč](?:ak|ka|ku|kom)|lunch",
    "dinner": r"ve[cč]er(?:a|e|u|i|om)|dinner|supper",
    "snack": r"u[zž]in(?:a|e|u|i|om)|snacks?",
}
_SLOT_PHRASE = re.compile(
    r"(?:\b(?:za|na|u|for|at|as)\s+(?:my\s+|moj\s+)?)?\b("
    + "|".join(f"(?P<{slot}>{forms})" for slot, forms in _SLOT_FORMS.items())
    + r")\b\s*[:\-–]?",
    re.IGNORECASE,
)
# What people say before the food itself: "dodaj …", "I had …", "pojeo sam …"
_LEADING = re.compile(
    r"^\s*(?:dodaj(?:te)?|upi[sš]i|zapi[sš]i|unesi|add|log|i\s+(?:had|ate)|had|ate"
    r"|(?:po)?jeo\s+sam|(?:po)?jela\s+sam|imao\s+sam|imala\s+sam)\b[\s,:]*",
    re.IGNORECASE,
)
# Joining words left dangling at either end once the slot is taken out
_DANGLING = re.compile(r"^(?:\s|,|;|\band\b|\bi\b)+|(?:\s|,|;|\band\b|\bi\b)+$", re.IGNORECASE)


def take_slot(text: str) -> tuple[str | None, str]:
    """The slot a sentence names, and the sentence without it — so
    "dodaj 200 g piletine i 100 g pirinča za ručak" is lunch, and the rest is
    only the food: "200 g piletine i 100 g pirinča"."""
    slot = None
    match = _SLOT_PHRASE.search(text or "")
    if match:
        slot = next(name for name in _SLOT_FORMS if match.group(name))
        text = text[: match.start()] + " " + text[match.end() :]
    text = _LEADING.sub("", text or "")
    text = _DANGLING.sub("", re.sub(r"\s+", " ", text))
    return slot, text.strip()


def slot_for_time(at: time | None) -> str | None:
    """The hours a meal is usually eaten in; None when it was not timed."""
    if at is None:
        return None
    if at.hour < 11:
        return "breakfast"
    if at.hour < 16:
        return "lunch"
    if at.hour < 21:
        return "dinner"
    return "snack"


def slot_in_words(text: str | None) -> str | None:
    """The first slot word in a title or a sentence, if there is one."""
    for word in re.findall(r"[a-z]+", normalize(text)):
        if word in _WORD_SLOT:
            return _WORD_SLOT[word]
    return None


def slot_of(slot: str | None, at: time | None, title: str | None) -> str:
    """What the page said, else the clock, else the name, else a snack."""
    if slot in SLOTS:
        return slot
    return slot_for_time(at) or slot_in_words(title) or "snack"
