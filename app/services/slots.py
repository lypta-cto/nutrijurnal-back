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
