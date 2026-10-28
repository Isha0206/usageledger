"""Check live URLs: fetch robots.txt and the page, then evaluate.

This is for spot checks of a handful of sites, not for crawling. It is
deliberately polite:

* robots.txt is fetched first, and the page is only fetched if robots.txt
  allows this tool's own product token (``usageledger``). If robots.txt is
  unreachable (5xx or network error) the page is not fetched either, as
  RFC 9309 requires.
* One request per URL, a clear User-Agent, timeouts, and size caps.

Uses only the standard library.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

from . import __version__
from .provenance import Policy, ProvenanceRecord, evaluate_page
from .robots import MAX_ROBOTS_BYTES, RobotsStore, origin_of
from .warc import Page

__all__ = ["USER_AGENT", "PRODUCT_TOKEN", "AI_CRAWLERS", "Fetch", "CheckResult", "fetch", "check_url", "format_result"]

PRODUCT_TOKEN = "usageledger"
USER_AGENT = f"{PRODUCT_TOKEN}/{__version__} (+https://github.com/Isha0206/usageledger; AI usage-preference checker)"
MAX_PAGE_BYTES = 5 * 1024 * 1024
TIMEOUT = 15
MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)

# Product tokens of well-known AI-related crawlers, for the access summary.
AI_CRAWLERS = (
    "GPTBot",
    "ClaudeBot",
    "CCBot",
    "Google-Extended",
    "Applebot-Extended",
    "PerplexityBot",
    "Bytespider",
    "meta-externalagent",
    "Amazonbot",
)


@dataclass
class Fetch:
    url: str
    final_url: str
    status: int | None  # None = network error
    header_lines: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    error: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses instead of following them, so every hop of a page
    redirect can be checked against its own robots.txt first."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, max_bytes: int = MAX_PAGE_BYTES, timeout: float = TIMEOUT, follow_redirects: bool = True) -> Fetch:
    """GET ``url``. robots.txt fetches follow redirects (RFC 9309 allows it);
    page fetches pass ``follow_redirects=False`` and handle each hop."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    try:
        opener = urllib.request.urlopen if follow_redirects else _NO_REDIRECT_OPENER.open
        with opener(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
            lines = [(k.lower(), v) for k, v in resp.headers.items()]
            return Fetch(url, resp.geturl(), resp.status, lines, body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(max_bytes)
        except Exception:
            body = b""
        lines = [(k.lower(), v) for k, v in (exc.headers.items() if exc.headers else [])]
        return Fetch(url, url, exc.code, lines, body)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return Fetch(url, url, None, error=str(reason))


@dataclass
class CheckResult:
    url: str
    final_url: str
    robots_status: int | None
    robots_error: str | None
    page_fetched: bool
    page_status: int | None
    page_error: str | None
    skipped_reason: str | None
    record: ProvenanceRecord
    crawler_access: dict[str, bool] | None  # token -> may crawl this URL

    def to_dict(self) -> dict:
        d = self.record.to_dict()
        d["live_check"] = {
            "final_url": self.final_url,
            "robots_status": self.robots_status,
            "robots_error": self.robots_error,
            "page_fetched": self.page_fetched,
            "page_status": self.page_status,
            "page_error": self.page_error,
            "skipped_reason": self.skipped_reason,
            "crawler_access": self.crawler_access,
            "user_agent_sent": USER_AGENT,
        }
        return d


def _load_robots(store: RobotsStore, origin: str, now: datetime, cache: dict) -> Fetch:
    if origin not in cache:
        res = fetch(origin + "/robots.txt", MAX_ROBOTS_BYTES)
        # A network error counts as "unreachable" (RFC 9309), stored as status 0.
        store.add(origin, res.body if res.status and 200 <= res.status < 300 else b"", now, res.status or 0)
        cache[origin] = res
    return cache[origin]


def check_url(
    url: str,
    policy: Policy | None = None,
    *,
    store: RobotsStore | None = None,
    fetch_page: bool = True,
    _cache: dict | None = None,
) -> CheckResult:
    """Fetch robots.txt (then, if allowed, the page) and evaluate it now."""
    policy = policy or Policy()
    store = store if store is not None else RobotsStore()
    cache = _cache if _cache is not None else {}
    now = datetime.now(timezone.utc)

    page = Page(url=url, fetched_at=now, body=b"")
    page_status = page_error = skipped = None
    fetched = False
    current = url
    robots = _load_robots(store, origin_of(current), now, cache)

    if not fetch_page:
        skipped = "page fetch disabled"
    else:
        for hop in range(MAX_REDIRECTS + 1):
            robots = _load_robots(store, origin_of(current), now, cache)
            ours = store.lookup(current, now, PRODUCT_TOKEN)
            if ours.status == "unreachable":
                skipped = "robots.txt unreachable, so the page is not fetched (RFC 9309)"
                break
            if ours.result is not None and not ours.result.crawlable:
                skipped = f"robots.txt does not allow {PRODUCT_TOKEN} to fetch this page"
                break
            res = fetch(current, follow_redirects=False)
            page_status, page_error = res.status, res.error
            location = dict(res.header_lines).get("location")
            if res.status in _REDIRECT_CODES and location:
                current = urljoin(current, location)
                continue
            if res.status is not None:
                fetched = True
                page = Page(url=current, fetched_at=now, body=res.body, http_status=res.status, header_lines=res.header_lines)
            break
        else:
            skipped = f"more than {MAX_REDIRECTS} redirects"
        if not fetched:
            page = Page(url=current, fetched_at=now, body=b"")
    final_url = current

    record = evaluate_page(page, store, policy)
    if not fetched:
        record.content_sha256 = None
        record.est_tokens = None

    access = None
    snap = store.lookup(final_url, now, "*")
    if snap.status == "ok" and snap.robots is not None:
        access = {tok: snap.robots.evaluate(final_url, tok).crawlable for tok in AI_CRAWLERS}
    elif snap.status == "unavailable":
        access = {tok: True for tok in AI_CRAWLERS}  # no robots.txt: nothing is blocked

    return CheckResult(
        url=url,
        final_url=final_url,
        robots_status=robots.status,
        robots_error=robots.error,
        page_fetched=fetched,
        page_status=page_status,
        page_error=page_error,
        skipped_reason=skipped,
        record=record,
        crawler_access=access,
    )


def format_result(r: CheckResult) -> str:
    """Human-readable summary of one check."""
    rec = r.record
    lines = [r.url]
    if r.final_url != r.url:
        lines.append(f"  redirected to {r.final_url}")

    robots_txt = {"ok": "found", "unavailable": "not found", "unreachable": "unreachable"}.get(
        (rec.robots or {}).get("status", "missing"), "missing"
    )
    rs = f"{robots_txt} ({r.robots_status})" if r.robots_status else f"{robots_txt} ({r.robots_error or 'no response'})"
    if r.page_fetched:
        ps = f"{r.page_status}"
    elif r.page_error:
        ps = f"error: {r.page_error}"
    else:
        ps = f"not fetched: {r.skipped_reason}"
    lines.append(f"  robots.txt: {rs}   page: {ps}")

    header = rec.header
    hdr = header["raw"] if header else "none"
    if header and not header.get("parse_ok", True):
        hdr += "  (malformed, counts as no preference)"
    robots_stmt = ((rec.robots or {}).get("statement") or {}).get("raw")
    lines.append(f"  Content-Usage   header: {hdr}   robots.txt: {robots_stmt or 'none'}")
    cs = rec.content_signal
    lines.append(f"  Content-Signal  robots.txt: {cs['raw'] if cs else 'none'}")
    tdm = rec.tdmrep.get("reservation")
    noai = [k for k in ("noai", "noimageai") if rec.noai.get(k)]
    lic = rec.license.get("spdx") or rec.license.get("url") or "none"
    lines.append(
        f"  TDMRep: {'reserved' if tdm == 1 else 'not reserved' if tdm == 0 else 'none'}   "
        f"noai: {', '.join(noai) or 'none'}   license: {lic}"
    )
    tt = (rec.training_tokens or {}).get("blocked")
    if tt is not None:
        lines.append(f"  Training-only tokens blocked: {', '.join(tt) or 'none'}")
    if r.crawler_access is not None:
        blocked = [t for t, ok in r.crawler_access.items() if not ok]
        allowed = [t for t, ok in r.crawler_access.items() if ok]
        lines.append(f"  AI crawlers blocked here: {', '.join(blocked) or 'none'}")
        lines.append(f"  AI crawlers allowed here: {', '.join(allowed) or 'none'}")

    who = rec.policy.get("user_agent", "*")
    verdict = "KEEP" if rec.keep else "DROP"
    why = "" if rec.keep else " - " + "; ".join(rec.reasons)
    if rec.keep and rec.combined.get(rec.policy.get("purpose", "train-ai")) == "unknown" and not (
        cs and cs["prefs"].get(rec.policy.get("purpose")) == "allow"
    ):
        why = " - no preference stated"
    lines.append(f"  Decision for {rec.policy.get('purpose')} as {who}: {verdict}{why}")
    return "\n".join(lines)
