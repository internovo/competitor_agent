"""Generates the Malad West demo fixtures. Run: python data/fixtures/build_fixtures.py
Coordinates are offsets from Marina64 chosen to give the distances shown in the reference screenshots."""
import json
from pathlib import Path

HERE = Path(__file__).parent
OWN_LAT, OWN_LNG = 19.1860, 72.8400
KM_LAT, KM_LNG = 0.008993, 0.009520  # degrees per km at 19 N

CAT = {
    "sport_fitness": ["Swimming Pool", "Gymnasium", "Jogging Track", "Cricket Net", "Yoga Deck", "Squash Court", "Badminton Court",
                      "Kids Pool", "Cycling Track", "Tennis Court", "Indoor Games"],
    "social_leisure": ["Clubhouse", "Sky Deck", "Multipurpose Hall", "Kids Play Area", "Senior Citizen Corner", "Co-Working Lounge",
                       "Amphitheatre", "Landscaped Garden", "Party Lawn", "Mini Theatre", "Library", "Barbecue Deck", "Pet Park"],
    "convenience_safety": ["EV Charging Bay", "CCTV Surveillance", "Power Backup", "Rain Water Harvesting", "Visitor Parking",
                           "24x7 Security", "Intercom", "High Speed Lifts", "Fire Fighting System", "Sewage Treatment Plant", "Solar Panels"],
}
LOOKUP = {name: cat for cat, names in CAT.items() for name in names}


def amen(*names):
    return [{"name": n, "category": LOOKUP[n], "scope": "project"} for n in names]


def pos(dlat_km=0.0, dlng_km=0.0):
    return round(OWN_LAT + dlat_km * KM_LAT, 6), round(OWN_LNG + dlng_km * KM_LNG, 6)


# ---------------------------------------------------------------- own project
marina = {
    "id": "marina64", "name": "Marina64", "builder": "Mahindra Lifespaces",
    "address": "Marve Road, Malad West, Mumbai 400064", "locality": "Malad West", "lat": OWN_LAT, "lng": OWN_LNG,
    "configurations": [1, 2, 3, 4], "carpet_sqft": {"min_sqft": 450, "max_sqft": 1650},
    "rate_psf": {"min_psf": 37500, "max_psf": 38500, "basis": "base"}, "possession": "2029-12-01",
    "structure": {"building_type": "multi_tower", "towers": 3, "floors_min": 30, "floors_max": 35, "land_acres": 2.5, "total_units": 640},
    "rera_phases": [{"number": "P51800055512", "verified": True, "label": "Phase 1"}],
    "amenities": amen("Swimming Pool", "Gymnasium", "Jogging Track", "Yoga Deck", "Kids Pool", "Clubhouse", "Sky Deck", "Multipurpose Hall",
                      "Kids Play Area", "Landscaped Garden", "Co-Working Lounge", "Mini Theatre", "EV Charging Bay", "CCTV Surveillance",
                      "Power Backup", "Rain Water Harvesting", "Visitor Parking", "24x7 Security", "High Speed Lifts", "Solar Panels"),
    "launched": "2025-03-01",
}
(HERE / "marina64.json").write_text(json.dumps(marina, indent=2))

# ------------------------------------------------- projects already on propOG
k_lat, k_lng = pos(0.7, 0)
kalpataru = {
    "id": "kalpataru-aurum", "name": "Kalpataru Aurum", "builder": "Kalpataru Ltd",
    "address": "Mamlatdar Wadi, Malad West, Mumbai 400064", "locality": "Malad West", "lat": k_lat, "lng": k_lng,
    "configurations": [3, 4], "carpet_sqft": {"min_sqft": 980, "max_sqft": 1420},
    "rate_psf": {"min_psf": 41200, "max_psf": 41200, "basis": "base"}, "possession": "2030-09-01",
    "structure": {"building_type": "multi_tower", "towers": 3, "floors_min": 28, "floors_max": 34, "land_acres": 1.8, "total_units": 420},
    "rera_phases": [{"number": "P51800051004", "verified": True, "label": "Phase 1"}, {"number": "P51800053377", "verified": True, "label": "Phase 2"}],
    "amenities": amen("Swimming Pool", "Gymnasium", "Jogging Track", "Squash Court", "Badminton Court", "Yoga Deck", "Tennis Court",
                      "Clubhouse", "Kids Play Area", "Co-Working Lounge", "Amphitheatre", "Landscaped Garden", "Party Lawn", "Library",
                      "Barbecue Deck", "EV Charging Bay", "CCTV Surveillance", "Power Backup", "Rain Water Harvesting", "Visitor Parking",
                      "24x7 Security", "Intercom"),
    "launched": "2024-11-01",
}
(HERE / "propog_projects.json").write_text(json.dumps([marina, kalpataru], indent=2))

# --------------------------------------------------------------- competitors
def cand(name, builder=None, rera=None, at=None, source="fixture", **kw):
    lat, lng = at if at else (None, None)
    return {"name": name, "builder": builder, "rera_no": rera, "lat": lat, "lng": lng, "source": source, "locality": "Malad West", **kw}


def entry(source, facts, url=None, fetched_at="2026-08-22T09:00:00+00:00"):
    return {"source": source, "url": url or f"https://example.test/{source}", "fetched_at": fetched_at, "facts": facts}


candidates = [
    cand("Runwal Vertex", "Runwal Group", "P51800048221", pos(0.4, 0), source="maharera",
         address="Marve Road, Malad West", nearest_metro="Kurar · 1.2 km", source_url="https://maharera.maharashtra.gov.in/project-summary-report-detail?registration_no=P51800048221"),
    cand("Vertex by Runwal", None, None, None, source="tavily", source_url="https://www.squareyards.com/mumbai-residential-property/runwal-vertex-malad-west-npd-1"),
    cand("Rustomjee Crest", "Rustomjee", None, pos(0, 0.9), source="places", address="Link Road, Malad West", nearest_metro="Malad · 0.8 km"),
    cand("Ajmera Skyline West", "Ajmera Realty", None, pos(-1.1, 0), source="places", address="Chincholi Bunder, Malad West"),
    cand("Sheth Nova", "Sheth Creators", None, pos(0, -1.3), source="places", address="Orlem, Malad West"),
    cand("Chandak Highscape", "Chandak Group", "P51800049880", pos(1.4, 0), source="maharera", address="Kanchpada, Malad West",
         source_url="https://maharera.maharashtra.gov.in/project-summary-report-detail?registration_no=P51800049880"),
    # to be dropped
    cand("Oberoi Sky City", "Oberoi Realty", "P51800012000", pos(3.2, 0), source="maharera", address="Borivali East"),
    cand("Evershine Cosmic", "Evershine Builders", None, pos(0, 0.6), source="places", address="Malad West"),
    cand("Lodha Marquee", "Lodha", "P51800031111", pos(-0.8, 0), source="maharera", address="Malad West"),
]

facts = {
    "P51800048221": [
        entry("maharera", {"name": "Runwal Vertex", "builder": "Runwal Group", "status": "under_construction", "configurations": [2, 3],
                           "carpet_min_sqft": 690, "carpet_max_sqft": 1105, "possession": "2029-03-31", "building_type": "multi_tower",
                           "towers": 4, "floors_min": 32, "floors_max": 38, "land_acres": 2.1, "rera_numbers": ["P51800048221"],
                           "launched": "2024-08-01", "extensions_filed": 0},
              url="https://maharera.maharashtra.gov.in/project-summary-report-detail?registration_no=P51800048221", fetched_at="2026-08-30T06:00:00+00:00"),
        entry("builder_site", {"name": "Runwal Vertex", "rate_min_psf": 35400, "rate_max_psf": 35400, "rate_basis": "base", "configurations": [2, 3],
                               "amenities": amen("Swimming Pool", "Gymnasium", "Jogging Track", "Cricket Net", "Yoga Deck", "Clubhouse", "Sky Deck",
                                                 "Multipurpose Hall", "Kids Play Area", "Senior Citizen Corner", "EV Charging Bay", "CCTV Surveillance",
                                                 "Power Backup", "Rain Water Harvesting", "Visitor Parking", "24x7 Security", "Intercom",
                                                 "High Speed Lifts", "Fire Fighting System")},
              url="https://www.runwal.com/vertex", fetched_at="2026-08-22T09:00:00+00:00"),
        entry("squareyards", {"name": "Runwal Vertex", "configurations": [2, 3], "rate_min_psf": 35000, "rate_max_psf": 36000, "rate_basis": "base",
                              "possession": "2029-03"}, url="https://www.squareyards.com/mumbai-residential-property/runwal-vertex-malad-west-npd-1"),
    ],
    "Rustomjee Crest": [
        entry("squareyards", {"name": "Rustomjee Crest", "builder": "Rustomjee", "status": "under_construction", "configurations": [2, 3],
                              "rate_min_psf": 29100, "rate_max_psf": 29100, "rate_basis": "undisclosed", "possession": "2029-06", "towers": 2,
                              "building_type": "multi_tower"}, url="https://www.squareyards.com/mumbai-residential-property/rustomjee-crest-npd-2"),
        entry("housing", {"name": "Rustomjee Crest", "configurations": [2, 3], "rate_min_psf": 33500, "rate_max_psf": 33500, "rate_basis": "undisclosed",
                          "possession": "2029-06"}, url="https://housing.com/in/buy/projects/page/rustomjee-crest"),
        entry("tavily", {"name": "Rustomjee Crest", "rate_min_psf": 38700, "rate_max_psf": 38700, "rate_basis": "undisclosed",
                         "amenities": amen("Swimming Pool", "Gymnasium", "Clubhouse", "Kids Play Area", "Landscaped Garden", "Yoga Deck",
                                           "Indoor Games", "Multipurpose Hall", "CCTV Surveillance", "Power Backup", "Visitor Parking",
                                           "24x7 Security", "Intercom", "High Speed Lifts")},
              url="https://www.99acres.com/rustomjee-crest-malad-west", fetched_at="2026-05-02T09:00:00+00:00"),
    ],
    "Ajmera Skyline West": [
        entry("builder_site", {"name": "Ajmera Skyline West", "builder": "Ajmera Realty", "status": "new_launch", "configurations": [1, 2, 3],
                               "carpet_min_sqft": 452, "carpet_max_sqft": 940, "possession": "2031-12", "building_type": "single", "towers": 1,
                               "floors_min": 40, "floors_max": 40, "launched": "2026-06-01",
                               "amenities": amen("Swimming Pool", "Gymnasium", "Clubhouse", "Kids Play Area", "Sky Deck", "EV Charging Bay",
                                                 "CCTV Surveillance", "Power Backup", "Rain Water Harvesting", "24x7 Security")},
              url="https://www.ajmera.com/skyline-west"),
    ],
    "Sheth Nova": [
        entry("housing", {"name": "Sheth Nova", "builder": "Sheth Creators", "status": "under_construction", "possession": "2030-03", "towers": 2,
                          "building_type": "multi_tower"}, url="https://housing.com/in/buy/projects/page/sheth-nova"),
    ],
    "P51800049880": [
        entry("maharera", {"name": "Chandak Highscape", "builder": "Chandak Group", "status": "under_construction", "configurations": [1, 2, 3],
                           "carpet_min_sqft": 430, "carpet_max_sqft": 1010, "possession": "2029-09-30", "building_type": "multi_tower",
                           "towers": 2, "floors_min": 24, "floors_max": 28, "land_acres": 1.2, "rera_numbers": ["P51800049880"],
                           "launched": "2025-01-01", "extensions_filed": 1},
              url="https://maharera.maharashtra.gov.in/project-summary-report-detail?registration_no=P51800049880", fetched_at="2026-08-30T06:00:00+00:00"),
        entry("squareyards", {"name": "Chandak Highscape", "rate_min_psf": 33800, "rate_max_psf": 34600, "rate_basis": "base",
                              "amenities": amen("Swimming Pool", "Gymnasium", "Jogging Track", "Clubhouse", "Kids Play Area", "Landscaped Garden",
                                                "Multipurpose Hall", "CCTV Surveillance", "Power Backup", "Rain Water Harvesting", "Visitor Parking",
                                                "24x7 Security", "High Speed Lifts", "Fire Fighting System", "Sewage Treatment Plant", "Intercom")},
              url="https://www.squareyards.com/mumbai-residential-property/chandak-highscape-npd-3"),
    ],
    "P51800012000": [entry("maharera", {"name": "Oberoi Sky City", "status": "under_construction", "configurations": [3, 4], "possession": "2029-12"})],
    "Evershine Cosmic": [entry("squareyards", {"name": "Evershine Cosmic", "status": "ready", "configurations": [1, 2], "carpet_min_sqft": 380,
                                              "carpet_max_sqft": 620, "possession": "2024-06", "towers": 1, "building_type": "single"})],
    "P51800031111": [entry("maharera", {"name": "Lodha Marquee", "status": "under_construction", "configurations": [2, 3], "carpet_min_sqft": 600,
                                        "carpet_max_sqft": 950, "possession": "2025-06-30", "towers": 3, "building_type": "multi_tower",
                                        "rera_numbers": ["P51800031111"]})],
}
(HERE / "competitors_malad_west.json").write_text(json.dumps({"candidates": candidates, "facts": facts}, indent=2))
print("fixtures written")
