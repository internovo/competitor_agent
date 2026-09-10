"""A compare must answer the same question whether or not the run is still in memory.

A run lives here two hours and is lost on restart; propOG stores every finished scan
forever. So the comparison a rep gets from last month's scan has to be the comparison
they would have got on the day -- not a thinner one that looks complete.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_api import REQUEST, _poll

OWN, RUNWAL, RUSTOMJEE, THIN = "Marina64", "P51800048221", "rustomjee-crest", "sheth-nova"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def run_id(client):
    r = client.post("/scans", json=REQUEST)
    assert r.status_code == 202, r.text
    return _poll(client, r.json()["run_id"])


@pytest.fixture(scope="module")
def stored(client, run_id):
    """What propOG keeps: the GET /scans/{run_id} body, verbatim."""
    return client.get(f"/scans/{run_id}").json()


def cols(stored, *ids):
    cc = stored["compare_columns"]
    return [cc["own"]] + [cc["competitors"][i] for i in ids]


# --- the columns are stored at all -------------------------------------------

def test_a_finished_scan_carries_the_compare_columns(stored):
    cc = stored["compare_columns"]
    assert set(cc) == {"own", "competitors"}
    assert cc["own"]["is_own"] is True
    assert RUNWAL in cc["competitors"] and RUSTOMJEE in cc["competitors"]


def test_only_projects_a_rep_can_analyse_get_a_column(stored):
    """An absent id and a disabled Analyse button say the same thing."""
    cc = stored["compare_columns"]["competitors"]
    for card in stored["competitors"]:
        assert (card["id"] in cc) == card["analyse_enabled"], card["id"]
    assert THIN not in cc


def test_a_column_carries_what_a_card_cannot(stored):
    """The four areas card() loses. This is why compare cannot run off cards."""
    col = stored["compare_columns"]["competitors"][RUNWAL]
    card = next(c for c in stored["competitors"] if c["id"] == RUNWAL)
    assert "amenities" in col and "amenities" not in card
    assert col["rera_phases"] and "rera_phases" not in card
    assert set(col["structure"]) > {"towers", "building_type"}
    assert col["carpet"]["source"] and "source" not in str(card["carpet_sqft"])


# --- the guarantee ------------------------------------------------------------

def test_stateless_compare_equals_the_run_scoped_one(client, run_id, stored):
    """The whole point. Same two projects, same answer, run in memory or not."""
    live = client.post(f"/scans/{run_id}/compare", json={"competitor_ids": [RUNWAL, RUSTOMJEE]})
    free = client.post("/compare", json={"columns": cols(stored, RUNWAL, RUSTOMJEE),
                                         "radius_km": stored["radius_km"]})
    assert live.status_code == 200 and free.status_code == 200, free.text
    assert free.json() == live.json()


def test_one_competitor_is_a_valid_comparison(client, stored):
    r = client.post("/compare", json={"columns": cols(stored, RUNWAL), "radius_km": 1.5})
    assert r.status_code == 200, r.text
    assert [c["name"] for c in r.json()["columns"]] == [OWN, "Runwal Vertex"]


# --- insight_source, always ---------------------------------------------------

@pytest.mark.parametrize("route", ["live", "stateless"])
def test_every_compare_says_who_wrote_the_insight(client, run_id, stored, route):
    """Absent reads as the pessimistic answer, which would call a model insight a
    template. It is emitted on the happy path too, not only on the fallback."""
    r = (client.post(f"/scans/{run_id}/compare", json={"competitor_ids": [RUNWAL]}) if route == "live"
         else client.post("/compare", json={"columns": cols(stored, RUNWAL), "radius_km": 1.5}))
    assert r.json()["insight_source"] in ("llm", "template")


def test_with_no_model_the_insights_are_template_written_and_say_so(client, stored):
    r = client.post("/compare", json={"columns": cols(stored, RUNWAL), "radius_km": 1.5})
    assert r.json()["insight_source"] == "template"      # the suite runs with no keys
    assert r.json()["insights"]["rate"]


# --- the spend is counted -----------------------------------------------------

def test_a_compare_reports_what_it_spent(client, stored):
    cost = client.post("/compare", json={"columns": cols(stored, RUNWAL), "radius_km": 1.5}).json()["cost"]
    assert set(cost) >= {"llm_calls", "input_tokens", "output_tokens", "estimated_inr", "model"}
    assert cost["llm_calls"] == 0 and cost["estimated_inr"] == 0.0   # no model configured here
    assert "not a bill" in cost["note"] or "no model" in cost["note"]


# --- refusals -----------------------------------------------------------------

def test_a_thin_column_is_refused_by_name(client, run_id, stored):
    """The 422 is the backstop; propOG gates the button on analyse_enabled first."""
    thin = next(c for c in stored["competitors"] if c["id"] == THIN)
    col = dict(stored["compare_columns"]["own"], id=THIN, name=thin["name"],
               label="THIN", completeness=thin["completeness"], is_own=False)
    r = client.post("/compare", json={"columns": [stored["compare_columns"]["own"], col], "radius_km": 1.5})
    assert r.status_code == 422 and "THIN" in r.text and thin["name"] in r.text


def test_an_unverified_column_is_refused(client, stored):
    col = dict(stored["compare_columns"]["competitors"][RUNWAL], label="UNVERIFIED")
    r = client.post("/compare", json={"columns": [stored["compare_columns"]["own"], col], "radius_km": 1.5})
    assert r.status_code == 422 and "lifecycle" in r.text


@pytest.mark.parametrize("n", [1, 4])
def test_only_one_or_two_competitors(client, stored, n):
    c = cols(stored, RUNWAL)
    body = {"columns": c[:1] if n == 1 else c + [c[1], c[1], c[1]], "radius_km": 1.5}
    assert client.post("/compare", json=body).status_code == 422


def test_a_column_of_the_wrong_shape_is_400_not_500(client, stored):
    """A card sent where a column belongs. Name what is missing, do not crash."""
    r = client.post("/compare", json={"columns": [stored["compare_columns"]["own"], {"id": "x", "name": "X"}],
                                      "radius_km": 1.5})
    assert r.status_code == 400 and "compare_columns" in r.text
