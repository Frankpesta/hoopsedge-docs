"""Team-name normalization (docs/03-data-ingestion.md section 2.5)."""

import re
import unicodedata

_APOSTROPHES = re.compile(r"['’`]")
_NOT_WORD = re.compile(r"[^a-z0-9]+")
_REPLACEMENTS = {"state": "st", "saint": "st", "&": "and"}
_DROPPED = frozenset({"university", "univ", "u"})


def normalize_name(name: str) -> str:
    """Lowercase, strip accents and punctuation, and unify st/state/saint and &/and.

    State and Saint both become "st" on purpose: sources write either, and
    `registry.build_aliases` refuses any alias that lands on two teams.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    text = _APOSTROPHES.sub("", text).replace("&", " & ")
    tokens = _NOT_WORD.sub(" ", text.replace(" & ", " and ")).split()
    return " ".join(_REPLACEMENTS.get(t, t) for t in tokens if t not in _DROPPED)
