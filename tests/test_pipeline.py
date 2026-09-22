"""Whole graph in fixture mode: no network, no LLM."""
import pytest

from app import service
from app.logic import compare


@pytest.fixture(scope="module")
def scan():
    import asyncio, json
    from app.config import FIXTURE_DIR
    from app.models.schema import OwnProject

    own = OwnProject(**json.loads((FIXTURE_DIR / "marina64.json").read_text()))
    rec, meta = asyncio.run(service.run_scan(own, 1.5, "fixture", llm=None))
    return own, rec, meta, None


def test_counts_and_labels(scan):
    own, rec, meta, _ = scan
    pl = service.list_payload(rec, own, meta)
    assert pl["counts"] == {"candidates_seen": 9, "eligible": 6, "comparable": 2, "partial": 2, "thin": 1, "unverified": 1}
    by = {c["name"]: c for c in pl["competitors"]}
    assert by["Runwal Vertex"]["label"] == "COMPARABLE" and by["Runwal Vertex"]["match_score"] is not None
    assert by["Rustomjee Crest"]["completeness"] == 4 and by["Rustomjee Crest"]["match_score"] is None
    assert by["Sheth Nova"]["label"] == "THIN" and by["Sheth Nova"]["analyse_enabled"] is False
    assert by["Kalpataru Aurum"]["on_propog"] is True


def test_a_propog_row_no_outside_source_names_is_unverified_and_unranked(scan):
    """Malad West, 21 September: "Kalpataru Aurum" was the #1 competitor at 54/80.

    It does not exist. The only project with that name is in Baner, Pune. It was a
    hand-written demo record in the propOG fixture, and it won because `on_propog`
    meant "trusted, not researched": its form was complete while real competitors'
    public data is patchy, so a fabrication outscored every building that is real.

    The fixture entry is deliberately still here. It is the case, and this is it
    turned into a test: complete, 0.54 km away, inside the radius, and no outside
    source names it in Malad West -- so it is shown and never scored.
    """
    own, rec, meta, _ = scan
    pl = service.list_payload(rec, own, meta)
    k = next(c for c in pl["competitors"] if c["name"] == "Kalpataru Aurum")
    assert k["completeness"] == 6          # the form is full, and it still does not count
    assert k["label"] == "UNVERIFIED"
    assert k["match_score"] is None and k["score_note"] == "not scored at this coverage"
    assert k["rank"] != 1
    assert k["data_note"] == "Listed on propOG, not found in public sources"
    assert k["analyse_enabled"] is False
    assert k["id"] not in pl["compare_columns"]["competitors"]
    # Nothing above it in the table is worse than it is.
    assert [c["label"] for c in pl["competitors"]][: k["rank"]] ==            sorted([c["label"] for c in pl["competitors"]][: k["rank"]], key=service.LABEL_ORDER.get)


def test_duplicate_candidate_merged(scan):
    _, rec, _, _ = scan
    names = [p.name for p in rec.projects]
    assert "Vertex by Runwal" not in names
    vertex = next(p for p in rec.projects if p.name == "Runwal Vertex")
    assert set(vertex.discovered_via) == {"maharera", "tavily"}


def test_drops_have_reasons(scan):
    own, rec, meta, _ = scan
    pl = service.list_payload(rec, own, meta)
    reasons = {d["name"]: d["reason"] for d in pl["dropped"]}
    assert "status is ready" in reasons["Evershine Cosmic"]
    assert "not after today" in reasons["Lodha Marquee"]
    assert "outside radius" in reasons["Oberoi Sky City"]


def test_conflict_shows_full_span(scan):
    own, rec, meta, _ = scan
    pl = service.list_payload(rec, own, meta)
    r = next(c for c in pl["competitors"] if c["name"] == "Rustomjee Crest")["rate_psf"]
    # squareyards outranks housing and tavily, so its number is published and the other
    # two travel beside it rather than taking the field away.
    assert (r["min"], r["max"], r["sources"]) == (29100, 29100, 3)
    assert "squareyards says 29,100" in r["conflict"]
    assert "housing says 33,500" in r["conflict"] and "tavily says 38,700" in r["conflict"]


def test_every_value_has_provenance_and_nothing_is_estimated(scan):
    _, rec, _, _ = scan
    for p in rec.projects:
        for f in ("configurations", "carpet_sqft", "rate_psf", "possession", "structure", "rera_phases", "amenities"):
            for fv in getattr(p, f):
                assert fv.prov.source and fv.prov.fetched_at
        if p.eligible:
            assert all(not getattr(p, f) for f in p.could_not_verify)


def test_thin_project_was_retried_once(scan):
    _, rec, meta, _ = scan
    nova = next(p for p in rec.projects if p.name == "Sheth Nova")
    assert nova.retried is True
    assert sum("retry:" in l for l in meta["log"]) == 1


def test_rera_verified_from_register(scan):
    _, rec, _, _ = scan
    vertex = next(p for p in rec.projects if p.name == "Runwal Vertex")
    phases = vertex.display("rera_phases").value
    assert phases[0].number == "P51800048221" and phases[0].verified


def test_detail_payload_shape(scan):
    own, rec, _, _ = scan
    vertex = next(p for p in rec.projects if p.name == "Runwal Vertex")
    d = service.detail_payload(vertex, own, rec)
    assert d["amenities"]["count"] == 19 and set(d["amenities"]["groups"]) == {"Sport & fitness", "Social & leisure", "Convenience & safety"}
    assert d["building"]["towers"] == 4 and d["building"]["floors"] == [32, 38]
    assert d["rera"]["verified_count"] == 1 and len(d["rera"]["phases"]) == 3
    assert d["timeline"]["launched"] == "2024-08-01" and d["timeline"]["possession_rera_proposed"] == "2029-03-31"
    assert d["timeline"]["months_to_handover"] > 0 and 0 < d["timeline"]["progress"] < 1
    assert d["provenance"]["sources_consulted"] == 3 and d["provenance"]["fields_with_conflict"] == 0
    assert d["location"]["nearest_metro"] == "Kurar · 1.2 km"


def test_compare_own_plus_two(scan):
    own, rec, _, _ = scan
    by = {p.name: p for p in rec.projects}
    payload = compare.build(own, [by["Runwal Vertex"], by["Rustomjee Crest"]], 1.5)
    assert [c["name"] for c in payload["columns"]] == ["Marina64", "Runwal Vertex", "Rustomjee Crest"]
    assert payload["columns"][0]["is_own"] is True
    assert payload["common_bhk"] == 3
    assert {p["name"] for p in payload["rate_axis"]["points"]} == {"Marina64", "Runwal Vertex"}
    assert payload["rate_axis"]["off_axis"][0]["name"] == "Rustomjee Crest"
    assert "squareyards says 29,100" in payload["rate_axis"]["off_axis"][0]["reason"]
    ra = payload["rate_axis"]
    own_mid = (own.rate_psf.min_psf + own.rate_psf.max_psf) / 2
    vertex_mid = 35400
    assert ra["baseline"] == {"id": own.id, "name": own.name, "value": round(own_mid)}
    assert {p["id"]: p["delta_pct"] for p in ra["points"]} == {
        own.id: 0.0, by["Runwal Vertex"].id: round((vertex_mid - own_mid) / own_mid * 100, 1)}
    assert payload["carpet"]["overlap"] == [690, 1105]
    assert payload["config_matrix"]["types"] == [1, 2, 3, 4]
    assert payload["structure"]["all_same_type"] is True
    assert payload["amenities"]["counts"][0] == 20 and payload["amenities"]["counts"][1] == 19
    assert payload["amenities"]["stale"][0]["id"] == "rustomjee-crest"


def test_no_own_base_rate_means_no_delta_rather_than_a_guessed_one(scan):
    """A percentage needs a baseline. Without one the cell stays empty."""
    own, rec, _, _ = scan
    own = own.model_copy(deep=True)
    own.rate_psf.basis = "all_in"
    ra = compare.build(own, [next(p for p in rec.projects if p.name == "Runwal Vertex")], 1.5)["rate_axis"]
    assert ra["baseline"] is None
    assert all(p["delta_pct"] is None for p in ra["points"])
    assert "no delta" in ra["baseline_note"]


def test_manual_override_lifts_completeness(scan):
    own, rec, _, db = scan
    nova = next(p for p in rec.projects if p.name == "Sheth Nova")
    assert nova.label == "THIN"
    service.apply_override(nova, own, 1.5, "rera_phases", ["P51800077777"])
    service.apply_override(nova, own, 1.5, "configurations", [2, 3])
    assert nova.completeness == 4 and nova.label == "PARTIAL"
    assert nova.display("rera_phases").prov.source == "manual"
    with pytest.raises(ValueError):
        service.apply_override(nova, own, 1.5, "amenities", [])


def test_the_agent_stores_nothing(scan):
    """The canonical copy lives in Postgres, behind the API's tenancy check in one
    function. The agent must never learn what a builder is."""
    from app.config import ROOT

    offenders = [p for p in (ROOT / "app").rglob("*.py") if "sqlite3" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"the agent still owns a database: {offenders}"
    _, rec, meta, _ = scan
    assert rec.scan_id and any("persist:" in line for line in meta["log"])


def test_the_table_is_capped_and_the_rest_goes_to_also_found(scan, monkeypatch):
    """Ten rows is what the UI shows. Sixty-one is a database dump."""
    from app.config import settings

    own, rec, meta, _ = scan
    monkeypatch.setattr(settings, "max_table_rows", 3)
    pl = service.list_payload(rec, own, meta)
    assert len(pl["competitors"]) == 3
    assert len(pl["also_found"]) == len(service.rank(rec.projects)) - 3
    assert set(pl["also_found"][0]) == {"id", "name", "distance_km", "label", "completeness", "reason"}


def test_coverage_outranks_distance_inside_a_label(own):
    """The bug this fixes: an empty register row 60 m away sorted above a filled launch
    a kilometre out, so the top of the table was the emptiest part of it."""
    from app.models.schema import Project

    near_empty = Project(id="a", name="Near Empty", status="under_construction", distance_km=0.06,
                        completeness=0, label="THIN", pages_seen=["https://x/a"])
    far_full = Project(id="b", name="Far Full", status="under_construction", distance_km=1.4,
                       completeness=4, label="PARTIAL", pages_seen=["https://x/b"])
    assert [p.id for p in service.rank([near_empty, far_full])] == ["b", "a"]


def test_a_project_no_source_wrote_about_is_listed_not_tabled(own):
    """Borivali West's top ten was 'Mahendra Jamnadas Kara', 'K Mehta And Company',
    'Bharat V Kenny' -- register filings with coordinates and nothing else. They are
    still reported; they are not competitors."""
    from app.models.schema import Project, ScanRecord

    read = Project(id="read", name="Has A Page", status="under_construction", distance_km=0.5,
                   completeness=4, label="PARTIAL", pages_seen=["https://x/read"])
    unread = Project(id="unread", name="Bharat V Kenny", status="unknown", distance_km=0.1,
                     completeness=1, label="UNVERIFIED")
    rec = ScanRecord(scan_id="s", own_id=own.id, radius_km=1.5, mode="live", projects=[read, unread])
    pl = service.list_payload(rec, own, {})
    assert [c["id"] for c in pl["competitors"]] == ["read"]
    beside = {x["id"]: x["reason"] for x in pl["also_found"]}
    assert beside["unread"] == "no source produced a page about this project"
