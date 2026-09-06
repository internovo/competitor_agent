"""Whole graph in fixture mode: no network, no LLM."""
import pytest

from app import service
from app.logic import compare
from app.storage.db import Database


@pytest.fixture(scope="module")
def scan(tmp_path_factory):
    import asyncio, json
    from app.config import FIXTURE_DIR
    from app.models.schema import OwnProject

    own = OwnProject(**json.loads((FIXTURE_DIR / "marina64.json").read_text()))
    db = Database(tmp_path_factory.mktemp("db") / "t.sqlite3")
    db.save_own(own)
    rec, meta = asyncio.run(service.run_scan(own, 1.5, "fixture", db, llm=None))
    return own, rec, meta, db


def test_counts_and_labels(scan):
    own, rec, meta, _ = scan
    pl = service.list_payload(rec, own, meta)
    assert pl["counts"] == {"candidates_seen": 9, "eligible": 6, "comparable": 3, "partial": 2, "thin": 1}
    by = {c["name"]: c for c in pl["competitors"]}
    assert by["Runwal Vertex"]["label"] == "COMPARABLE" and by["Runwal Vertex"]["match_score"] is not None
    assert by["Rustomjee Crest"]["completeness"] == 4 and by["Rustomjee Crest"]["match_score"] is None
    assert by["Sheth Nova"]["label"] == "THIN" and by["Sheth Nova"]["analyse_enabled"] is False
    assert by["Kalpataru Aurum"]["on_propog"] is True


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
    assert (r["min"], r["max"], r["sources"]) == (29100, 38700, 3)
    assert "3 sources disagree" in r["conflict"]


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
    assert payload["rate_axis"]["off_axis"][0]["name"] == "Rustomjee Crest" and "disagree" in payload["rate_axis"]["off_axis"][0]["reason"]
    assert payload["carpet"]["overlap"] == [690, 1105]
    assert payload["config_matrix"]["types"] == [1, 2, 3, 4]
    assert payload["structure"]["all_same_type"] is True
    assert payload["amenities"]["counts"][0] == 20 and payload["amenities"]["counts"][1] == 19
    assert payload["amenities"]["stale"][0]["id"] == "rustomjee-crest"


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


def test_persisted_and_readable(scan):
    own, rec, _, db = scan
    latest = db.latest_scan(own.id)
    assert latest is not None and latest[0].scan_id == rec.scan_id
    assert db.find_project("P51800048221")[1].name == "Runwal Vertex"
