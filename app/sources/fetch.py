"""One door for every HTTP call, with a live/replay cache behind it.

live   -> hit the network, write data/cache/<key>.body + .meta.json
replay -> read only from the cache; a miss is a loud error, never a silent empty page

Live reuses a cached page only while it is younger than CACHE_MAX_AGE_HOURS and only
if it was a success. An error was cached the same as a page until Sep 2026, so 31
"API key not valid" replies from one broken afternoon were still being served as
"no buildings nearby" days after the key was fixed. Replay is unaffected: it must
serve whatever the run recorded, errors included, or the frozen corpus stops
reproducing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import CACHE_DIR, settings


class CacheMiss(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    fetched_at: datetime
    from_cache: bool

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}


# The paid APIs a scan depends on, by host. A refusal from one of these is not "no results":
# it is a source that did not run, and the scan is incomplete.
API_SOURCES = {"api.tavily.com": "tavily", "places.googleapis.com": "places", "maps.googleapis.com": "places"}
KEY_REFUSED = {401: "API key rejected", 403: "API key rejected", 402: "usage limit reached",
               429: "rate limited", 432: "usage limit reached"}


class Fetcher:
    def __init__(self, mode: str, cache_dir: Path = CACHE_DIR, timeout: float = 30.0):
        assert mode in ("live", "replay", "fixture")
        self.mode = mode
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.calls: list[str] = []
        # source -> {"status", "reason"}, first refusal only. Tavily answered 432 to every
        # call on 17 Sep and the search code read that as an empty result list: the scan
        # finished, looked normal, and two localities lost every Tavily page unannounced.
        self.refused: dict[str, dict] = {}

    def _note(self, res: FetchResult) -> FetchResult:
        host = res.url.split("//")[-1].split("/")[0]
        source = API_SOURCES.get(host)
        if source and not res.ok and source not in self.refused:
            reason = KEY_REFUSED.get(res.status) or ("API key rejected" if "api key" in res.text.lower() else None)
            if reason:
                self.refused[source] = {"source": source, "status": res.status, "reason": reason}
        return res

    # ---- cache -----------------------------------------------------------
    @staticmethod
    def _key(method: str, url: str, params: dict | None, body: Any, headers: dict | None) -> str:
        # Keys must not include secrets: strip obvious key headers/params before hashing.
        clean_params = {k: v for k, v in (params or {}).items() if "key" not in k.lower()}
        clean_headers = {k: v for k, v in (headers or {}).items() if "key" not in k.lower() and k.lower() != "authorization"}
        raw = json.dumps([method, url, clean_params, body, clean_headers], sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _usable(self, cached: FetchResult) -> bool:
        """Replay takes the recording as it stands; live wants a fresh success."""
        if self.mode != "live":
            return True
        if not cached.ok:
            return False
        age_h = (datetime.now(timezone.utc) - cached.fetched_at).total_seconds() / 3600
        return age_h < settings.cache_max_age_hours

    def _read(self, key: str) -> FetchResult | None:
        meta = self.cache_dir / f"{key}.meta.json"
        body = self.cache_dir / f"{key}.body"
        if not (meta.exists() and body.exists()):
            return None
        m = json.loads(meta.read_text())
        return FetchResult(url=m["url"], status=m["status"], text=body.read_text(encoding="utf-8"),
                           fetched_at=datetime.fromisoformat(m["fetched_at"]), from_cache=True)

    def _write(self, key: str, res: FetchResult, fetcher: str) -> None:
        (self.cache_dir / f"{key}.body").write_text(res.text, encoding="utf-8")
        (self.cache_dir / f"{key}.meta.json").write_text(json.dumps({
            "url": res.url, "status": res.status, "fetched_at": res.fetched_at.isoformat(), "fetcher": fetcher,
        }, indent=2))

    # ---- public ----------------------------------------------------------
    async def request(self, url: str, *, method: str = "GET", params: dict | None = None, json_body: Any = None,
                      headers: dict | None = None, impersonate: bool = False) -> FetchResult:
        key = self._key(method, url, params, json_body, headers)
        self.calls.append(url)
        cached = self._read(key)
        if cached is not None and self._usable(cached):
            return self._note(cached)
        if self.mode != "live":
            raise CacheMiss(f"{method} {url} not in cache (mode={self.mode})")

        if impersonate:
            res = await self._curl_cffi(url, method, params, json_body, headers)
            fetcher = "curl_cffi"
        else:
            res = await self._httpx(url, method, params, json_body, headers)
            fetcher = "httpx"
        # Only successes are kept. A failure that is cached is a failure that is
        # permanent, and the next scan should get to try the network again.
        if res.ok:
            self._write(key, res, fetcher)
        return self._note(res)

    async def get(self, url: str, **kw) -> FetchResult:
        return await self.request(url, method="GET", **kw)

    async def post(self, url: str, **kw) -> FetchResult:
        return await self.request(url, method="POST", **kw)

    # ---- backends --------------------------------------------------------
    async def _httpx(self, url, method, params, json_body, headers) -> FetchResult:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers={**DEFAULT_HEADERS, **(headers or {})}) as c:
            r = await c.request(method, url, params=params, json=json_body)
            return FetchResult(url=str(r.url), status=r.status_code, text=r.text, fetched_at=datetime.now(timezone.utc), from_cache=False)

    async def _curl_cffi(self, url, method, params, json_body, headers) -> FetchResult:
        from curl_cffi.requests import AsyncSession  # imported lazily; only needed live

        async with AsyncSession(impersonate="chrome", timeout=self.timeout) as s:
            r = await s.request(method, url, params=params, json=json_body, headers={**DEFAULT_HEADERS, **(headers or {})}, allow_redirects=True)
            return FetchResult(url=str(r.url), status=r.status_code, text=r.text, fetched_at=datetime.now(timezone.utc), from_cache=False)


def html_to_text(html: str, max_chars: int) -> str:
    """Strip navigation, scripts and styles; keep readable text with light structure."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    lines = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "span", "div"]):
        t = el.get_text(" ", strip=True)
        if t and len(t) < 600 and (not lines or lines[-1] != t):
            lines.append(t)
    text = "\n".join(lines)
    return text[:max_chars]
