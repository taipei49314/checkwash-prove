"""Text from the repository, made safe to print on a terminal."""
from __future__ import annotations

import unicodedata

# Joiners shape emoji and Arabic, Persian or Indic names, and move or hide nothing.
_KEEP = frozenset("‌‍")


def visible(text: str) -> str:
    """text with every control, format, line-separator and paragraph-separator character escaped, as \\uXXXX or,
    above U+FFFF, \\UXXXXXXXX: an escape sequence, a bidirectional override or a line break in it then cannot act
    on a terminal or start a line of its own."""
    out = []
    for c in text:
        if c not in _KEEP and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp"):
            out.append(f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
        else:
            out.append(c)
    return "".join(out)
