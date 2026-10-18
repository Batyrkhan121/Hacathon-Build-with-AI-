"""In-memory data store: POIs + embedding index + landmarks. Owner: CORE lane (task T22).

Index format (produced by the DATA lane, pipeline/build_index.py):
  data/index/embeddings.npy  float32 (N, D), L2-normalised, row i <-> ids[i]
  data/index/ids.json        list[str] of POI ids, same order
  data/index/meta.json       {"model": str, "dim": int, "count": int, "created_at": str, ...}
If the index is missing, meta.model != settings.embed_model or meta.dim != settings.embed_dim
-> embeddings=None
(retrieval falls back to lexical scoring) and a warning is logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.config import Settings
from app.schemas import POI, Landmark


@dataclass
class Store:
    pois: list[POI]
    by_id: dict[str, POI]
    landmarks: dict[str, Landmark]
    embeddings: np.ndarray | None = None  # (N, D) aligned with `pois`
    embed_model: str | None = None
    snapshot_date: str | None = None  # max(poi.fetched_at)
    warnings: list[str] = field(default_factory=list)


def load_pois(path: Path) -> list[POI]:
    """Read a JSONL file of POI (utf-8). Skip and count invalid lines; never crash on a bad row."""
    raise NotImplementedError("T22 (CORE)")


def load_landmarks(path: Path) -> dict[str, Landmark]:
    raise NotImplementedError("T22 (CORE)")


def load_store(settings: Settings, poi_path: Path | None = None) -> Store:
    """Build the Store. Align embeddings rows to `pois` order via ids.json."""
    raise NotImplementedError("T22 (CORE)")
