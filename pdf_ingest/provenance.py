"""Deterministic evidence identity and explicit coordinate transformations."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .schema import Origin, SourceSpec


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_block_id(source: SourceSpec, origins: list[Origin], anchor: Any, text: str, fingerprint: str) -> str:
    record = {
        "tenant_id": source.tenant_id,
        "source_id": source.source_id,
        "source_version": source.source_version,
        "origins": [o.model_dump(mode="json") for o in origins],
        "anchor": anchor,
        "text_sha256": text_hash(text),
        "conversion_fingerprint": fingerprint,
    }
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "blk-" + text_hash(payload)


def normalize_bbox(bbox, width: float, height: float, origin: str = "bottom-left", rotation: int = 0) -> tuple[float, float, float, float]:
    """Transform an unrotated page box into displayed top-left normalized space.

    ``bbox`` uses ascending x/y endpoints. Rotation is the clockwise PDF display
    rotation. Values outside the page are rejected rather than silently clipped.
    """
    if origin not in {"bottom-left", "top-left"}:
        raise ValueError("unsupported coordinate origin")
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("rotation must be a right angle")
    if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
        raise ValueError("page dimensions must be finite and positive")
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise ValueError("box must have four finite coordinates")
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 <= x1 <= width and 0 <= y0 <= y1 <= height):
        raise ValueError("box lies outside page or has reversed corners")
    if origin == "bottom-left":
        y0, y1 = height - y1, height - y0
    points = [(x0 / width, y0 / height), (x1 / width, y0 / height),
              (x0 / width, y1 / height), (x1 / width, y1 / height)]
    def rotate(x, y):
        return {0: (x, y), 90: (1 - y, x), 180: (1 - x, 1 - y), 270: (y, 1 - x)}[rotation]
    rotated = [rotate(x, y) for x, y in points]
    return (min(p[0] for p in rotated), min(p[1] for p in rotated),
            max(p[0] for p in rotated), max(p[1] for p in rotated))
