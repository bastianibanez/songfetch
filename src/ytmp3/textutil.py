"""Pure text helpers shared by search, ranking and catalog scanning.

No I/O lives here, so every function is directly unit-testable.
"""

from __future__ import annotations

import re
import unicodedata

# Characters we fold to a space rather than deleting, so "AR/CO" -> "ar co"
# and "qwerty_lxd" -> "qwerty lxd" instead of becoming one glued token.
_SEPARATORS = re.compile(r"[\s/_\\|,;:+&\-–—.()\[\]{}<>\"“”«»!?*=@#$%^~`]+")
# Characters deleted outright: apostrophes ("Don't" -> "dont", "Rockin'" -> "rockin")
# and symbol marks like ® ™ © that carry no search value.
_DELETED = re.compile(r"['’‘`´]")
# Unicode categories dropped after NFKD: combining marks (diacritics) and
# "symbol, other" (®, ™, ©, emoji).
_DROP_CATEGORIES = frozenset({"Mn", "So"})

_WS = re.compile(r"\s+")


def normalize(s: str) -> str:
    """Fold a string to its searchable form.

    - NFKD decompose, then drop combining marks: ``Körper`` -> ``korper``,
      ``sorèd`` -> ``sored``.
    - Drop standalone symbols: ``swedm®`` -> ``swedm``.
    - Delete apostrophes so ``Don't`` -> ``dont``.
    - Turn other punctuation into spaces so ``A.F.T.E.R`` -> ``a f t e r``
      and ``AR/CO`` -> ``ar co``.
    - Casefold and collapse whitespace.

    CJK and other non-Latin scripts survive unchanged: ``Flame 火炎`` -> ``flame 火炎``.
    """
    if not s:
        return ""
    s = _DELETED.sub("", s)
    decomposed = unicodedata.normalize("NFKD", s)
    kept = "".join(ch for ch in decomposed if unicodedata.category(ch) not in _DROP_CATEGORIES)
    kept = _SEPARATORS.sub(" ", kept)
    return _WS.sub(" ", kept).strip().casefold()


def tokens(s: str) -> list[str]:
    """Normalized whitespace-separated tokens."""
    n = normalize(s)
    return n.split() if n else []


def squash(s: str) -> str:
    """Normalized with all spaces removed.

    Lets ``aftr`` match ``A.F.T.E.R`` and ``talktome`` match ``Talk To Me``.
    """
    return normalize(s).replace(" ", "")


_UNSAFE_FS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_filename(s: str, max_len: int = 180) -> str:
    """Filesystem-safe name that still reads like the original title."""
    cleaned = _UNSAFE_FS.sub("_", s)
    cleaned = _WS.sub(" ", cleaned).strip().strip(".")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned or "untitled"
