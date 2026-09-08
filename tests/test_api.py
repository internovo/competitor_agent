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
    assert body["counts"] == {"candidates_seen": 9, "eligible": 6, "comparable": 3, "partial": 2, "thin": 1, "unverified": 0}
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
