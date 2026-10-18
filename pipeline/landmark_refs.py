"""Which OSM object and Wikidata item each landmark in data/landmarks.json stands for.

Plain data, no imports: used by verify_landmarks (OSM centres), normalize (landmarks as
POIs) and enrich (Wikidata descriptions). Looked up on 2026-10-18.
"""

from __future__ import annotations

LANDMARKS_CHECKED_AT = "2026-10-18"

# landmark id -> OSM object whose centre is the anchor
LANDMARK_OSM: dict[str, str] = {
    "expo": "way/454857501",  # the Nur Alem sphere, tagged in OSM as "Alem.ai" (office)
    "mega_silk_way": "way/499328514",  # shop=mall, building
    "baiterek": "way/230401645",  # historic=monument
    "khan_shatyr": "way/460703779",  # shop=mall
    "palace_peace": "way/166197368",  # amenity=community_centre (Pyramid)
    "hazrat_sultan": "way/240860325",  # place_of_worship
    "airport_nqz": "way/166500591",  # aeroway=terminal T1 (walk-relevant, not the runway)
    "astana_opera": "way/917478479",  # amenity=theatre
    "nurly_zhol": "way/1484667027",  # railway=station
}

# landmark id -> Wikidata item (for descriptions of landmarks that become POIs)
LANDMARK_WIKIDATA: dict[str, str] = {
    "expo": "Q123643894",  # Nur Alem EXPO Pavilion
    "baiterek": "Q804210",
    "khan_shatyr": "Q671484",
    "palace_peace": "Q2119741",
    "hazrat_sultan": "Q4494668",
    "astana_opera": "Q21371563",
    "nurly_zhol": "Q137551678",
}
