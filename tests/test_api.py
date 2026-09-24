"""The run-scoped API. The subject arrives in the request; nothing is stored."""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import FIXTURE_DIR
from app.main import app, runs
from app.models.schema import OwnProject
from app.storage.runs import RunStore

REQUEST = json.loads((FIXTURE_DIR / "scan_request.json").read_text())


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def run_id(client):
    r = client.post("/scans", json=REQUEST)
    assert r.status_code == 202, r.text
    return _poll(client, r.json()["run_id"])


def _poll(client, rid, want="done"):
    import time

    for _ in range(300):
        body = client.get(f"/scans/{rid}").json()
        if body["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert body["status"] == want, body.get("error")
    return rid


def test_health_says_what_it_is(client):
    body = client.get("/health").json()
    assert body["ok"] is True and body["provider"] == "anthropic"


def test_a_scan_is_accepted_immediately_with_a_run_id(client):
    """A live scan takes minutes. The endpoint must not be the thing that waits."""
    import time

    start = time.perf_counter()
    r = client.post("/scans", json=REQUEST)
    assert r.status_code == 202
    assert (time.perf_counter() - start) < 0.2
    assert len(r.json()["run_id"]) == 12
    assert r.json()["status"] in ("queued", "running")


def test_a_running_scan_reports_the_stage_it_is_in(client):
    r = client.post("/scans", json=REQUEST)
    rid = r.json()["run_id"]
    seen = set()
    for _ in range(300):
        body = client.get(f"/scans/{rid}").json()
        seen.add(body["stage"])
        if body["status"] in ("done", "failed"):
            break
    assert body["status"] == "done"
    # The node names are already meaningful, so the loading screen shows real work.
    assert any("discover" in s or "extract" in s for s in body["stages_done"][0:1] + body["stages_done"])


def test_a_failed_run_ends_failed_and_names_the_cause(client, monkeypatch):
    """A broken run must never present itself as an empty one."""
    from app import service
    from app.llm.client import LLMAuthError

    async def boom(own, radius_km, mode, **kw):
        raise LLMAuthError("the LLM rejected our credentials (AuthenticationError: 401)")

    monkeypatch.setattr(service, "run_scan", boom)
    rid = client.post("/scans", json=REQUEST).json()["run_id"]
    _poll(client, rid, want="failed")
    body = client.get(f"/scans/{rid}").json()
    assert body["error"]["type"] == "llm_auth"
    assert "credentials" in body["error"]["message"]
    assert body["error"]["stage"] == "extract"
    assert "competitors" not in body


def test_the_subject_arrives_in_the_request_not_from_a_lookup(client):
    """The agent must never learn what a builder is, so there is no own-projects
    table to look one up in."""
    assert client.get("/own-projects").status_code == 404
    body = dict(REQUEST)
    body["own"] = dict(REQUEST["own"], id="somebody-else", name="Another Tower")
    r = client.post("/scans", json=body)
    assert r.status_code == 202
    assert client.get(f"/scans/{r.json()['run_id']}").json()["own"]["name"] == "Another Tower"


def test_a_finished_run_carries_the_whole_list_payload(client, run_id):
    body = client.get(f"/scans/{run_id}").json()
    assert body["status"] == "done" and body["stage"] is None
    assert body["counts"] == {"candidates_seen": 9, "eligible": 6, "handing_over_before": 0,
                              "nearby_other_configurations": 0,
                              "comparable": 2, "partial": 2, "thin": 1, "unverified": 1}
    assert body["stages_done"] and any("discover[fixture]" in s for s in body["stages_done"])
    assert body["elapsed_s"] >= 0


def test_a_competitor_detail_is_run_scoped(client, run_id):
    d = client.get(f"/scans/{run_id}/competitors/P51800048221").json()
    assert d["name"] == "Runwal Vertex"
    assert client.get(f"/scans/{run_id}/competitors/nope").status_code == 404


def test_compare_keeps_the_shape_the_frontend_is_built_on(client, run_id):
    r = client.post(f"/scans/{run_id}/compare", json={"competitor_ids": ["P51800048221", "rustomjee-crest"]})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert [c["name"] for c in payload["columns"]] == ["Marina64", "Runwal Vertex", "Rustomjee Crest"]
    assert payload["rate_axis"]["off_axis"][0]["reason"]
    assert payload["config_matrix"]["types"] == [1, 2, 3, 4]
    for key in ("rate_axis", "off_axis", "config_matrix", "columns", "possession", "carpet",
                "structure", "amenities", "insights"):
        assert key in payload or key in payload["rate_axis"]


def test_a_thin_competitor_cannot_be_analysed(client, run_id):
    r = client.post(f"/scans/{run_id}/compare", json={"competitor_ids": ["sheth-nova"]})
    assert r.status_code == 422 and "THIN" in r.text


def test_a_rep_can_record_a_fact_from_a_site_visit(client, run_id):
    r = client.post(f"/scans/{run_id}/competitors/sheth-nova/fields",
                    json={"field": "rera_phases", "value": ["P51800077777"]})
    assert r.status_code == 200
    assert r.json()["rera"]["phases"][0]["number"] == "P51800077777"


def test_an_unknown_run_is_404(client):
    assert client.get("/scans/deadbeef1234").status_code == 404


def test_an_unfinished_run_will_not_serve_a_detail(client):
    run = runs.create(OwnProject(**REQUEST["own"]), 1.5, "fixture")
    assert client.get(f"/scans/{run.run_id}/competitors/x").status_code == 409


# --- the store itself -------------------------------------------------------

def test_the_store_is_bounded():
    store = RunStore(max_runs=3)
    own = OwnProject(**REQUEST["own"])
    ids = [store.create(own, 1.5, "fixture").run_id for _ in range(5)]
    assert len(store) == 3
    assert store.get(ids[0]) is None and store.get(ids[-1]) is not None


def test_the_store_forgets_runs_past_their_ttl():
    from datetime import timedelta

    store = RunStore(ttl_hours=1)
    run = store.create(OwnProject(**REQUEST["own"]), 1.5, "fixture")
    run.created_at -= timedelta(hours=2)
    assert store.get(run.run_id) is None


def test_restarting_the_process_loses_every_run():
    """Intended. The canonical copy lives in Postgres, written by the API."""
    own = OwnProject(**REQUEST["own"])
    first = RunStore()
    run_id = first.create(own, 1.5, "fixture").run_id
    assert RunStore().get(run_id) is None


# --- POST /scan: the route propOG's agent-client.cjs actually calls ---------
# The body below is what that client sends, verbatim from its JSON.stringify, with
# the subject copied off a real seeded Marina64 row -- including the two fields
# propOG's own routes.cjs currently gets wrong: `locality` never reaches the wire,
# and `configurations` is always empty because inventory carries `type`, not `config`.

AGENT_BODY = {
    "run_id": "6f1a2b3c-4d5e-4f60-8123-000000000001",
    "project_id": "b0000000-0000-4000-8000-000000000064",
    "builder_id": "a0000000-0000-4000-8000-000000000001",
    "radius_km": 3,
    "subject": {"id": "b0000000-0000-4000-8000-000000000064", "name": "Mahindra Marina64",
                "city": "Mumbai", "latitude": 19.1874, "longitude": 72.8401, "configurations": []},
}


def _post_scan(client, body=None, headers=None):
    return client.post("/scan", json=body or AGENT_BODY, headers=headers or {})


def test_scan_accepts_the_body_the_propog_client_sends_and_returns_immediately():
    """Their client aborts after 10s and reads only res.ok, so a 2xx that arrives
    fast is the whole response contract."""
    with TestClient(app) as c:
        r = _post_scan(c)
    assert r.status_code == 202


def test_the_run_is_keyed_by_the_id_propog_already_holds():
    """agent-client.cjs reads no identifier back, so a run addressed by an id the
    agent invented is a run Node can never poll."""
    with TestClient(app) as c:
        _post_scan(c)
        body = c.get(f"/scans/{AGENT_BODY['run_id']}").json()
    assert body["run_id"] == AGENT_BODY["run_id"]


def test_a_thin_subject_is_scanned_rather_than_rejected(monkeypatch):
    """No carpet, no rate, no possession, no structure, no configurations. The scan
    still runs; those dimensions are simply not scored against anything."""
    from app.main import _own_from_subject

    own = _own_from_subject(AGENT_BODY["subject"], AGENT_BODY["project_id"])
    assert (own.carpet_sqft, own.rate_psf, own.possession, own.structure) == (None, None, None, None)
    assert own.configurations == []
    assert (own.lat, own.lng) == (19.1874, 72.8401)
    assert own.address == "Mumbai"          # `city` is the only place name on the wire
    assert own.locality is None             # propOG's routes.cjs never sends it


def test_bhk_strings_are_read_as_bedroom_counts():
    """propOG's inventory carries "2 BHK"; this form counts bedrooms."""
    from app.sources.propog import bhk as _bhk

    assert _bhk(["2 BHK", "3 BHK", "2 BHK"]) == [2, 3]
    assert _bhk([2, 3]) == [2, 3]
    assert _bhk(["studio", None]) == []     # nothing numeric, so nothing is guessed


def test_a_subject_with_no_name_is_refused_with_a_readable_reason():
    """Their client reads 200 characters of the body on a non-2xx, so the reason has
    to fit and has to say what to fix."""
    body = {**AGENT_BODY, "subject": {**AGENT_BODY["subject"], "name": None}}
    with TestClient(app) as c:
        r = _post_scan(c, body)
    assert r.status_code == 422
    assert "name" in r.text


def test_a_subject_that_cannot_be_placed_is_refused():
    body = {**AGENT_BODY, "subject": {"id": "x", "name": "Draft Project"}}
    with TestClient(app) as c:
        r = _post_scan(c, body)
    assert r.status_code == 422
    assert "geocode" in r.text


def test_the_shared_secret_is_checked_only_when_one_is_configured(monkeypatch):
    """agent-client.cjs sends x-agent-token only when its own env var is non-empty,
    so this mirrors it exactly."""
    from app.config import settings

    with TestClient(app) as c:
        assert _post_scan(c).status_code == 202                       # unset: not checked
        monkeypatch.setattr(settings, "agent_token", "s3cret")
        assert _post_scan(c).status_code == 401                       # set, header absent
        assert _post_scan(c, headers={"x-agent-token": "wrong"}).status_code == 401
        assert _post_scan(c, headers={"x-agent-token": "s3cret"}).status_code == 202
