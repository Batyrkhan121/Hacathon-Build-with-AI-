"""How much of the walk graph OSM really describes: lighting, steps, crossings, kerbs.

    python -m pipeline.walk_coverage    # data/raw/osm_walk_*.json -> walk_coverage.json + table

The graph is built from the same raw files the same way as data/processed/walk_graph.npz
(the counts are checked against walk_graph_meta.json), and lighting and kerbs are read from
the same npz bits a reader sees (edge 2 lit=yes, 32 lit=no; node 4 lowered kerb, 16 kerb not
lowered; crossing 2 signals, 8 unmarked, 32 marked), so the shares here are what the route
can report. Edges are counted by length and by number, crossings and their kerbs by number
of crossing nodes.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.hours import ASTANA_TZ
from pipeline.normalize import load_payloads, osm_base
from pipeline.walk_graph import (
    EDGE_FOOTWAY,
    EDGE_LIT,
    EDGE_STEPS,
    EDGE_UNLIT,
    NODE_KERB,
    NODE_KERB_HIGH,
    NODE_MARKED,
    NODE_SIGNALS,
    NODE_UNMARKED,
    WalkGraph,
    build_graph,
    walk_files,
)

LIT_CLASSES = ("yes", "no", "unknown")
CROSSING_CLASSES = ("signals", "marked", "unmarked", "unknown")
KERB_CLASSES = ("lowered", "raised", "unknown")


def lit_class(edge_flags: int) -> str:
    """Bit 2 lit=yes, bit 32 lit=no, neither: no data (other lit values count here too)."""
    if edge_flags & EDGE_LIT:
        return "yes"
    return "no" if edge_flags & EDGE_UNLIT else "unknown"


def crossing_class(flags: int) -> str:
    """The reader's rule: bit 2 signals, else 8 unmarked, else 32 marked, else unknown."""
    if flags & NODE_SIGNALS:
        return "signals"
    if flags & NODE_UNMARKED:
        return "unmarked"
    return "marked" if flags & NODE_MARKED else "unknown"


def kerb_class(node_flags: int) -> str:
    """Bit 16 (a kerb not lowered on some side) wins over bit 4; neither: no data."""
    if node_flags & NODE_KERB_HIGH:
        return "raised"
    return "lowered" if node_flags & NODE_KERB else "unknown"


def _share(part: float, total: float) -> float:
    return round(part / total, 4) if total else 0.0


def _by_length(classes: Sequence[str], lengths: np.ndarray, names: Sequence[str]) -> dict:
    total = float(lengths.sum())
    labels = np.array(classes, dtype=object)
    out = {}
    for name in names:
        mask = labels == name
        km = float(lengths[mask].sum()) / 1000
        out[name] = {
            "km": round(km, 1),
            "length_share": _share(km * 1000, total),
            "edges": int(mask.sum()),
            "edges_share": _share(int(mask.sum()), len(classes)),
        }
    return out


def _by_count(classes: Sequence[str], names: Sequence[str]) -> dict:
    counts = Counter(classes)
    return {n: {"nodes": counts[n], "share": _share(counts[n], len(classes))} for n in names}


def measure(graph: WalkGraph) -> dict:
    lengths = graph.edge_len_m.astype(np.float64)
    lit = [lit_class(int(f)) for f in graph.edge_flags]
    footway = (graph.edge_flags & EDGE_FOOTWAY) != 0
    steps = (graph.edge_flags & EDGE_STEPS) != 0
    crossings = sorted(graph.crossing_tags)
    kerbs = [kerb_class(int(graph.node_flags[i])) for i in crossings]
    steps_len = float(lengths[steps].sum())
    return {
        "nodes": len(graph.node_lat),
        "edges": len(lengths),
        "length_km": round(float(lengths.sum()) / 1000, 1),
        "footway_length_km": round(float(lengths[footway].sum()) / 1000, 1),
        "lit": _by_length(lit, lengths, LIT_CLASSES),
        "lit_footways": _by_length([c for c, f in zip(lit, footway, strict=True) if f],
                                   lengths[footway], LIT_CLASSES),
        "steps": {
            "km": round(steps_len / 1000, 2),
            "length_share": _share(steps_len, float(lengths.sum())),
            "edges": int(steps.sum()),
            "edges_share": _share(int(steps.sum()), len(lengths)),
            # ramp=* / ramp:wheelchair=* / ramp:stroller=* known on the steps
            "with_ramp_tags": sum(
                1 for t, s in zip(graph.edge_tags, steps, strict=True)
                if s and any(k in t for k in ("ramp", "ramp:wheelchair", "ramp:stroller"))
            ),
        },
        "crossings_total": len(crossings),
        "crossings": _by_count(
            [crossing_class(int(graph.node_flags[i])) for i in crossings], CROSSING_CLASSES),
        "kerbs": _by_count(kerbs, KERB_CLASSES),
        "lit_values": dict(Counter(t["lit"] for t in graph.edge_tags if "lit" in t)
                           .most_common()),
        "kerb_values": dict(Counter(k for i in crossings for k in graph.crossing_kerbs.get(i, []))
                            .most_common()),
    }


def run(raw_dir: Path, processed_dir: Path) -> dict:
    paths = walk_files(raw_dir)
    if not paths:
        raise SystemExit(f"no osm_walk_tile_*.json in {raw_dir}: run pipeline.walk_graph first")
    payloads = load_payloads(paths)
    graph, _ = build_graph(payloads)
    cov = measure(graph)
    meta_path = processed_dir / "walk_graph_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        cov["same_graph_as_npz"] = (meta.get("nodes"), meta.get("edges")) == (
            cov["nodes"], cov["edges"])
    cov["osm_snapshot"] = min(osm_base(p) for p in payloads)
    cov["created_at"] = datetime.now(ASTANA_TZ).isoformat(timespec="seconds")
    out = processed_dir / "walk_coverage.json"
    out.write_text(json.dumps(cov, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cov


def main(argv: Sequence[str] | None = None) -> None:
    from pipeline.report import walk_coverage_lines

    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    settings = get_settings()
    cov = run(settings.data_dir / "raw", settings.data_dir / "processed")
    print("\n".join(walk_coverage_lines(cov)))


if __name__ == "__main__":
    main()
