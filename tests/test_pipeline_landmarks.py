"""pipeline.verify_landmarks without network."""

from __future__ import annotations

from pipeline.verify_landmarks import apply, lookup_query, osm_centers


def test_lookup_query_groups_ids_by_type() -> None:
    query = lookup_query(["way/1", "node/2", "way/3"])
    assert "way(id:1,3);" in query and "node(id:2);" in query
    assert query.rstrip().endswith("out center tags;")


def test_apply_moves_mapped_landmark_and_keeps_unmapped() -> None:
    payload = {"elements": [{"type": "way", "id": 230401645,
                             "center": {"lat": 51.128291, "lon": 71.430451}}]}
    landmarks = [
        {"id": "baiterek", "geo": {"lat": 51.1283, "lon": 71.4305}, "verified": False},
        {"id": "unknown_place", "geo": {"lat": 51.0, "lon": 71.0}, "verified": False},
    ]
    report = dict(apply(landmarks, osm_centers(payload)))
    assert landmarks[0]["geo"] == {"lat": 51.12829, "lon": 71.43045}
    assert landmarks[0]["verified"] is True
    assert report["baiterek"] is not None and report["baiterek"] < 10
    assert report["unknown_place"] is None and landmarks[1]["verified"] is False


def test_large_drift_is_not_applied() -> None:
    from pipeline.verify_landmarks import MAX_DRIFT_M

    payload = {"elements": [{"type": "way", "id": 230401645,
                             "center": {"lat": 51.15, "lon": 71.43045}}]}  # ~2.4 km off
    landmarks = [{"id": "baiterek", "geo": {"lat": 51.1283, "lon": 71.4305}, "verified": False}]
    report = dict(apply(landmarks, osm_centers(payload)))
    assert report["baiterek"] is not None and report["baiterek"] > MAX_DRIFT_M
    assert landmarks[0]["geo"] == {"lat": 51.1283, "lon": 71.4305}
    assert landmarks[0]["verified"] is False
