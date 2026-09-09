"""The frozen suburbs, replayed offline: no network, no model, no key.

Slow (it re-reads ~300 cached pages), so it is deselected from the default run:

    uv run pytest -q            # everything else, seconds
    uv run pytest -q -m slow    # this file

This is the regression surface the Anthropic budget bought and cannot buy again.
It pins what the deterministic half of the pipeline makes of real fetched pages,
so conflicts, merge, lifecycle, completeness, match score, eligibility, the sort
and the compare payload can all be changed with an answer to "did that help?".
"""
import asyncio
import json

import pytest

from app import service
from app.config import ROOT, settings
from app.logic import compare as compare_logic
from app.models.schema import OwnProject

pytestmark = pytest.mark.slow

FIXTURES = ROOT / "tests" / "fixtures" / "replay"


def _replay(name: str):
    own = OwnProject(**json.loads((FIXTURES / name / "subject.json").read_text(encoding="utf-8")))
    rec, meta = asyncio.run(service.run_scan(own, settings.default_radius_km, "replay", llm=None))
    return own, rec, meta, service.list_payload(rec, own, meta)


@pytest.fixture(scope="module")
def borivali():
    return _replay("borivali-west-2026-09-08")


def test_no_key_is_needed_and_no_model_is_built(borivali):
    """The conftest clears every key for the whole suite, so reaching this line at
    all is the proof: the run below used neither a provider nor the network."""
    assert settings.has_llm is False
    _, _, meta, _ = borivali
    assert meta["pages_fetched"] > 200        # all of it out of data/cache


def test_the_borivali_table_is_the_one_the_anthropic_budget_paid_for(borivali):
    _, _, _, payload = borivali
    assert payload["counts"]["eligible"] == 4      # was 5; the clothing shop left
    assert payload["counts"]["comparable"] == 2
    assert [c["name"] for c in payload["competitors"][:2]] == ["Sanghvi Horizon", "Avyukta Neelkamal"]
    assert payload["competitors"][0]["match_score"] is not None


def test_the_clothing_shop_is_beside_the_table_with_its_reason_not_deleted(borivali):
    _, _, _, payload = borivali
    assert "Pant Project Store" not in [c["name"] for c in payload["competitors"]]
    row = next(a for a in payload["also_found"] if "Pant Project Store" in a["name"])
    assert "clothing store" in row["reason"]
    assert "Pant" not in json.dumps(payload["dropped"])


def test_every_empty_cell_in_the_table_names_its_reason(borivali):
    """The whole product claim. A blank would be a guess with the ink left out."""
    from app.config import COMPLETENESS_FIELDS

    _, rec, _, payload = borivali
    for row in payload["competitors"]:
        p = next(x for x in rec.projects if x.id == row["id"])
        for f in COMPLETENESS_FIELDS:
            r = p.report(f)
            assert (r.absent is None) != (r.value is None), (row["name"], f)
            if r.absent:
                assert r.label, (row["name"], f)


def test_the_frozen_compare_contract_survives_the_replay(borivali):
    own, rec, _, payload = borivali
    picked = [next(x for x in rec.projects if x.id == c["id"]) for c in payload["competitors"][:2]]
    out = compare_logic.build(own, picked, settings.default_radius_km)
    assert [c["name"] for c in out["columns"]] == [own.name] + [p.name for p in picked]
    assert out["columns"][0]["is_own"] is True
    assert out["rate_axis"]["off_axis"][0]["reason"]
    assert out["config_matrix"]["types"] == [1, 2, 3]


def test_the_carpet_band_refuses_exactly_the_ranges_it_should(borivali):
    """Two in Borivali, both read off pages listing something larger than the
    building. Nothing plausible was taken."""
    _, rec, _, _ = borivali
    refused = {p.name: p.carpet_sqft.span for p in rec.projects
               if p.carpet_sqft.absent == "IMPLAUSIBLE_CARPET"}
    assert refused == {"Airavat By Bhoomi Group": [1184, 3734],
                       "H. Rishabraj Phoenix": [1161, 5031]}


def test_the_other_two_suburbs_replay_too_so_the_surface_is_not_one_locality():
    for name, pages in (("kandivali-west-2026-09-08", 100), ("goregaon-west-2026-09-08", 20)):
        _, _, meta, payload = _replay(name)
        assert meta["pages_fetched"] > pages, name
        assert payload["counts"]["eligible"] >= 1, name


def test_ten_concurrent_replays_of_the_real_corpus_agree_to_the_byte():
    """The loaded-machine version, on real cached pages rather than fixtures.

    Ten scans in one event loop is the starvation condition itself: they compete
    for the same CPU, and under the old wall-clock budget a candidate whose
    siblings were busy could be pushed past its deadline and print
    RESEARCH_TIMED_OUT where a quiet run printed the real reason. Goregaon West is
    the smallest corpus, so this costs about a minute rather than ten.
    """
    from app.models.schema import REPORTED_FIELDS
    from tests.test_invariants import _canonical

    def subject():
        return OwnProject(**json.loads((FIXTURES / "goregaon-west-2026-09-08" / "subject.json")
                                       .read_text(encoding="utf-8")))

    subjects = [subject() for _ in range(10)]

    async def ten():
        return await asyncio.gather(*(service.run_scan(o, settings.default_radius_km, "replay", llm=None)
                                      for o in subjects))

    runs = asyncio.run(ten())
    seen = {_canonical(rec, service.list_payload(rec, own, meta))
            for own, (rec, meta) in zip(subjects, runs)}
    assert len(seen) == 1
    assert not [p.id for rec, _ in runs for p in rec.projects for f in REPORTED_FIELDS
                if p.report(f).absent == "RESEARCH_TIMED_OUT"]
