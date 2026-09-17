"""The failures that print as an empty neighbourhood rather than as a failure.

A cached error, a cached page that has gone stale, and a Places reply nobody read
the status of all reach a sales rep the same way: a short table with nothing wrong
on the face of it.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.sources.fetch import CacheMiss, FetchResult, Fetcher

BAD_KEY = json.dumps({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                "status": "INVALID_ARGUMENT"}})
URL = "https://places.googleapis.com/v1/places:searchNearby"


def _fetcher(tmp_path, mode, status=200, text="ok"):
    """A fetcher whose network always answers `status`, so only the cache is under test."""
    f = Fetcher(mode, cache_dir=tmp_path)
    calls = []

    async def _fake(url, method, params, json_body, headers):
        calls.append(url)
        return FetchResult(url=url, status=status, text=text, fetched_at=datetime.now(timezone.utc), from_cache=False)

    f._httpx = _fake
    f.network = calls
    return f


def _age(fetcher, key_url, hours):
    """Backdate every cached entry, the way a cache that is a day old looks."""
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    for meta in fetcher.cache_dir.glob("*.meta.json"):
        m = json.loads(meta.read_text())
        m["fetched_at"] = when.isoformat()
        meta.write_text(json.dumps(m))


# --- a failure must not be cached -------------------------------------------

def test_a_failed_response_is_never_written_to_the_cache(tmp_path):
    f = _fetcher(tmp_path, "live", status=400, text=BAD_KEY)
    asyncio.run(f.post(URL, json_body={"q": 1}))
    assert list(tmp_path.glob("*.body")) == [], "a 400 was cached and will be served forever"


def test_a_success_is_still_cached(tmp_path):
    f = _fetcher(tmp_path, "live")
    asyncio.run(f.get("https://example.com/p"))
    asyncio.run(f.get("https://example.com/p"))
    assert len(f.network) == 1, "the second live call should have come off the cache"


def test_an_error_already_in_the_cache_is_ignored_live_and_refetched(tmp_path):
    """The 31 "API key not valid" replies from one broken afternoon."""
    poisoned = _fetcher(tmp_path, "live")
    poisoned._write(poisoned._key("POST", URL, None, {"q": 1}, None),
                    FetchResult(url=URL, status=400, text=BAD_KEY, fetched_at=datetime.now(timezone.utc), from_cache=False), "httpx")

    f = _fetcher(tmp_path, "live", status=200, text='{"places": []}')
    res = asyncio.run(f.post(URL, json_body={"q": 1}))
    assert f.network == [URL] and res.status == 200


# --- freshness ---------------------------------------------------------------

def test_live_refetches_a_page_older_than_the_limit(tmp_path):
    f = _fetcher(tmp_path, "live")
    asyncio.run(f.get("https://example.com/p"))
    _age(f, "https://example.com/p", settings.cache_max_age_hours + 1)
    asyncio.run(f.get("https://example.com/p"))
    assert len(f.network) == 2, "a stale page was served to a live scan as if it were today's"


def test_live_keeps_a_page_inside_the_limit(tmp_path):
    f = _fetcher(tmp_path, "live")
    asyncio.run(f.get("https://example.com/p"))
    _age(f, "https://example.com/p", settings.cache_max_age_hours / 2)
    asyncio.run(f.get("https://example.com/p"))
    assert len(f.network) == 1


def test_replay_serves_the_recording_as_it_stands(tmp_path):
    """Including errors and however old it is, or the frozen corpus stops reproducing."""
    seed = _fetcher(tmp_path, "live")
    seed._write(seed._key("GET", "https://example.com/p", None, None, None),
                FetchResult(url="https://example.com/p", status=406, text="blocked",
                            fetched_at=datetime.now(timezone.utc) - timedelta(days=365), from_cache=False), "httpx")

    res = asyncio.run(Fetcher("replay", cache_dir=tmp_path).get("https://example.com/p"))
    assert res.status == 406 and res.from_cache


def test_replay_still_raises_on_a_genuine_miss(tmp_path):
    with pytest.raises(CacheMiss):
        asyncio.run(Fetcher("replay", cache_dir=tmp_path).get("https://example.com/never-fetched"))


# --- Places reads its status -------------------------------------------------

def test_places_raises_on_an_error_body_instead_of_reporting_no_buildings(tmp_path):
    from app.sources.base import ScanContext
    from app.sources.places import PlacesSource
    from app.models.schema import OwnProject

    ctx = ScanContext(own=OwnProject(id="x", name="X", lat=19.2, lng=72.85), radius_km=1.5,
                      fetcher=_fetcher(tmp_path, "live", status=400, text=BAD_KEY))
    with pytest.raises(RuntimeError, match="API key not valid"):
        asyncio.run(PlacesSource().discover(ctx))


def test_places_still_returns_empty_when_the_api_genuinely_finds_nothing(tmp_path):
    from app.sources.base import ScanContext
    from app.sources.places import PlacesSource
    from app.models.schema import OwnProject

    ctx = ScanContext(own=OwnProject(id="x", name="X", lat=19.2, lng=72.85), radius_km=1.5,
                      fetcher=_fetcher(tmp_path, "live", status=200, text='{"places": []}'))
    assert asyncio.run(PlacesSource().discover(ctx)) == []


# --- one ceiling on concurrent scans -----------------------------------------

def test_scans_over_the_limit_wait_rather_than_all_starting_at_once():
    from app import service

    live = 0
    peak = 0

    async def body():
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    async def run():
        await asyncio.gather(*(service.execute(None) for _ in range(6)))

    original = service._execute
    service._execute = lambda run_: body()
    try:
        asyncio.run(run())
    finally:
        service._execute = original
    assert peak <= settings.max_concurrent_scans


# --- a live deployment cannot be left unauthenticated -------------------------

def test_live_mode_refuses_to_boot_without_the_shared_secret(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "fetch_mode", "live")
    monkeypatch.setattr(settings, "agent_token", None)
    with pytest.raises(RuntimeError, match="AGENT_TOKEN"):
        with TestClient(app):
            pass


def test_live_mode_boots_with_the_secret_set(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "fetch_mode", "live")
    monkeypatch.setattr(settings, "agent_token", "s3cret")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


# --- a refused key is a source that did not run, not an empty neighbourhood ---

def test_a_refused_search_quota_marks_the_scan_incomplete(tmp_path):
    """17 Sep: Tavily answered 432 to every call and the scan looked normal."""
    f = _fetcher(tmp_path, "live", status=432, text='{"detail":{"error":"This request exceeds your plan\'s set usage limit."}}')
    res = asyncio.run(f.post("https://api.tavily.com/search", json_body={"query": "x"}))
    assert not res.ok
    assert f.refused == {"tavily": {"source": "tavily", "status": 432, "reason": "usage limit reached"}}


def test_a_bad_places_key_is_named_even_as_a_400(tmp_path):
    f = _fetcher(tmp_path, "live", status=400, text=BAD_KEY)
    asyncio.run(f.post(URL, json_body={}))
    assert f.refused["places"]["reason"] == "API key rejected"


def test_a_page_that_errors_is_not_a_source_outage(tmp_path):
    """A builder site returning 403 is one thin project, not an incomplete scan."""
    f = _fetcher(tmp_path, "live", status=403, text="Forbidden")
    asyncio.run(f.get("https://www.some-builder.com/project"))
    assert f.refused == {}


def test_the_scan_payload_says_which_source_did_not_run():
    from app import service
    from app.models.schema import OwnProject, ScanRecord

    own = OwnProject(id="o", name="Marina64")
    rec = ScanRecord(scan_id="s", own_id="o", radius_km=1.5, mode="live", projects=[])
    down = [{"source": "tavily", "status": 432, "reason": "usage limit reached"}]
    payload = service.list_payload(rec, own, {"sources_unavailable": down})
    assert payload["incomplete"] is True and payload["sources_unavailable"] == down
    assert service.list_payload(rec, own, {})["incomplete"] is False
