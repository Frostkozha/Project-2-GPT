"""Conservative normalization: preserve wording, spacing, symbols and numbers."""
from __future__ import annotations

import unicodedata

NORMALIZATION_VERSION = "nfc-lf-v1"


def normalize_text(text: str) -> tuple[str, list[str]]:
    if not isinstance(text, str):
        raise TypeError("normalization requires text")
    changes: list[str] = []
    result = text.replace("\r\n", "\n").replace("\r", "\n")
    if result != text:
        changes.append("line_endings_lf")
    composed = unicodedata.normalize("NFC", result)
    if composed != result:
        changes.append("unicode_nfc")
    return composed, changes
