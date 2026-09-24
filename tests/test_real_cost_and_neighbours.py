"""A cached page is not a paid call, and a neighbour is not nothing.

A Malad West rerun on 24 Sep reported Rs 209.22 for a scan that made no network calls
at all, and a 1 BHK subject published one competitor while ten real buildings next door
went unmentioned.
"""
import asyncio
from datetime import datetime, timezone

from app.sources.fetch import CacheMiss, FetchResult, Fetcher


def _fetcher(tmp_path, status=200, text="ok"):
    f = Fetcher("live", cache_dir=tmp_path)

    async def _fake(url, method, params, json_body, headers):
        return FetchResult(url=url, status=status, text=text,
                           fetched_at=datetime.now(timezone.utc), from_cache=False)

    f._httpx = _fake
    return f


def test_a_second_request_for_the_same_page_is_a_hit_not_a_call(tmp_path):
    f = _fetcher(tmp_path)
    asyncio.run(f.get("https://example.com/p"))
    asyncio.run(f.get("https://example.com/p"))
    assert len(f.calls) == 2, "both attempts are recorded"
    assert len(f.outbound) == 1, "only one left the machine"
    assert f.hits == 1


def test_a_replay_miss_is_neither_a_hit_nor_a_call(tmp_path):
    """`calls - outbound` counted every replay miss as a cache hit."""

    f = Fetcher("replay", cache_dir=tmp_path)
    try:
        asyncio.run(f.get("https://example.com/never-recorded"))
    except CacheMiss:
        pass
    assert len(f.calls) == 1 and len(f.outbound) == 0 and f.hits == 0


def test_nothing_is_billed_for_a_run_served_entirely_from_disk(tmp_path):
    from app.cost import classify
    f = _fetcher(tmp_path)
    for _ in range(3):
        asyncio.run(f.get("https://api.tavily.com/search"))
    searches, places, _ = classify(f.outbound)
    assert searches == 1, "three requests, one search actually bought"
    assert f.hits == 2


# --- nearby, but selling something else -------------------------------------

def _dropped(reason: str, pages=("u",)):
    from app.models.schema import Project
    p = Project(id="p", name="Next Door", status="under_construction", distance_km=0.4)
    p.eligible, p.drop_reason, p.pages_seen = False, reason, list(pages)
    return p


def test_a_neighbour_dropped_only_on_configuration_is_kept_to_show():
    """`eligibility.check` returns on the FIRST rule a candidate fails, so a
    configuration reason means it is in radius, still selling and handing over in the
    future. The only thing wrong with it is the size of the flats."""
    from app.models.schema import ScanRecord
    from app.service import nearby_other_configurations
    rec = ScanRecord(scan_id="s", own_id="o", radius_km=1.5, mode="replay",
                     projects=[_dropped("no configuration overlap ([2] vs [1])")])
    assert [p.id for p in nearby_other_configurations(rec)] == ["p"]


def test_a_neighbour_dropped_for_anything_else_stays_out():
    from app.models.schema import ScanRecord
    from app.service import nearby_other_configurations
    for reason in ("status is ready, only new launch / under construction qualify",
                   "outside radius (1.82 km > 1.5 km)",
                   "possession 2020-01-01 is not after today",
                   "no coordinates"):
        rec = ScanRecord(scan_id="s", own_id="o", radius_km=1.5, mode="replay",
                         projects=[_dropped(reason)])
        assert nearby_other_configurations(rec) == [], reason


def test_a_neighbour_nobody_researched_is_not_shown():
    """No pages means no card worth rendering."""
    from app.models.schema import ScanRecord
    from app.service import nearby_other_configurations
    rec = ScanRecord(scan_id="s", own_id="o", radius_km=1.5, mode="replay",
                     projects=[_dropped("no configuration overlap ([2] vs [1])", pages=())])
    assert nearby_other_configurations(rec) == []
