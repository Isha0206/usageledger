"""Survey a list of sites and summarise how they signal AI preferences.

    usageledger survey sites.txt --csv survey.csv --report survey.md

``sites.txt`` has one site per line, optionally followed by a category, and
``#`` comments::

    # site                 category
    www.nzz.ch             news-ch
    www.ethz.ch            university-ch
    www.nytimes.com        news-intl

Each site gets the same polite live check as ``usageledger check``. Sites are
visited one after another with a pause in between.
"""

from __future__ import annotations

import csv
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from . import __version__
from .live import AI_CRAWLERS, CheckResult, check_url
from .provenance import Policy
from .robots import RobotsStore

__all__ = ["read_sites", "run_survey", "row_of", "write_csv", "summary_markdown", "POSTURES"]

POSTURES = ("blocks none", "blocks some", "blocks most", "blocks all", "unknown")


def read_sites(path: str) -> list[tuple[str, str]]:
    """Read ``site [category]`` lines; returns [(url, category), ...]."""
    sites = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.replace(",", " ").split()
            url = parts[0] if "://" in parts[0] else "https://" + parts[0]
            sites.append((url, parts[1] if len(parts) > 1 else ""))
    return sites


def posture(access: dict[str, bool] | None) -> str:
    if access is None:
        return "unknown"
    n = sum(1 for ok in access.values() if not ok)
    if n == 0:
        return "blocks none"
    if n == len(access):
        return "blocks all"
    return "blocks most" if n > len(access) / 2 else "blocks some"


def row_of(res: CheckResult, category: str = "") -> dict:
    """One flat row per site, for CSV and the summary."""
    rec = res.record
    robots = rec.robots or {}
    stmt = robots.get("statement") or {}
    access = res.crawler_access
    blocked = [t for t, ok in (access or {}).items() if not ok]
    tokens = (rec.training_tokens or {}).get("blocked")
    return {
        "site": res.url,
        "category": category,
        "final_url": res.final_url,
        "robots_txt": {"ok": "found", "unavailable": "not found"}.get(robots.get("status"), robots.get("status")),
        "page_status": res.page_status if res.page_fetched else (res.page_error or res.skipped_reason or ""),
        "posture": posture(access),
        "ai_crawlers_blocked": len(blocked) if access is not None else "",
        "ai_crawlers_checked": len(access) if access is not None else "",
        "blocked": ";".join(blocked),
        "training_tokens_blocked": ";".join(tokens) if tokens else "",
        "content_usage_header": (rec.header or {}).get("raw") or "",
        "content_usage_robots": stmt.get("raw") or "",
        "content_signal": (rec.content_signal or {}).get("raw") or "",
        "tdm_reservation": rec.tdmrep.get("reservation") if rec.tdmrep.get("reservation") is not None else "",
        "noai": ";".join(k for k in ("noai", "noimageai") if rec.noai.get(k)),
        "license": rec.license.get("spdx") or rec.license.get("url") or "",
        "decision": "keep" if rec.keep else "drop",
        "reasons": "; ".join(rec.reasons),
        **{f"allows_{t}": ("" if access is None else ("yes" if access[t] else "no")) for t in AI_CRAWLERS},
    }


def run_survey(
    sites: Iterable[tuple[str, str]],
    policy: Policy | None = None,
    *,
    fetch_page: bool = True,
    delay: float = 1.0,
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    sites = list(sites)
    store, cache = RobotsStore(), {}
    rows = []
    for i, (url, category) in enumerate(sites, 1):
        if i > 1 and delay:
            time.sleep(delay)
        res = check_url(url, policy, store=store, fetch_page=fetch_page, _cache=cache)
        row = row_of(res, category)
        rows.append(row)
        if progress:
            n = row["ai_crawlers_blocked"]
            detail = f"{n}/{row['ai_crawlers_checked']} AI crawlers blocked" if n != "" else row["robots_txt"]
            progress(f"[{i}/{len(sites)}] {url}: {row['posture']} ({detail})")
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def summary_markdown(rows: list[dict], policy: Policy | None = None) -> str:
    policy = policy or Policy()
    cats = sorted({r["category"] for r in rows if r["category"]})
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    known = [r for r in rows if r["posture"] != "unknown"]

    def cols(fn) -> str:
        cells = [fn(rows)] + [fn(by_cat[c]) for c in cats]
        return " | ".join(cells)

    head = "| | All | " + " | ".join(cats) + " |" if cats else "| | All |"
    sep = "|---|---:|" + "---:|" * len(cats)
    n_line = "| Sites | " + cols(lambda rs: str(len(rs))) + " |"

    out = [
        "# AI usage-preference survey",
        "",
        f"{len(rows)} sites checked on {datetime.now(timezone.utc):%Y-%m-%d} with usageledger {__version__}. "
        f"Crawler rules could be determined for {len(known)} of them (a missing robots.txt blocks nothing).",
        "",
        f"Crawlers checked: {', '.join(AI_CRAWLERS)}. Google-Extended and Applebot-Extended are not crawlers; "
        "they exist only to opt out of AI training.",
        "",
        "## How many AI crawlers each site blocks",
        "",
        head,
        sep,
        n_line,
    ]
    for p in POSTURES:
        if any(r["posture"] == p for r in rows):
            out.append(
                f"| {p} | "
                + cols(lambda rs, p=p: f"{sum(r['posture'] == p for r in rs)} ({_pct(sum(r['posture'] == p for r in rs), len(rs))})")
                + " |"
            )

    out += ["", "## Share of sites blocking each crawler", "", head, sep]
    for t in AI_CRAWLERS:
        def frac(rs, t=t):
            k = [r for r in rs if r["posture"] != "unknown"]
            b = sum(r[f"allows_{t}"] == "no" for r in k)
            return f"{b} ({_pct(b, len(k))})"

        out.append(f"| {t} | " + cols(frac) + " |")

    signals = [
        ("IETF Content-Usage (HTTP header)", lambda r: bool(r["content_usage_header"])),
        ("IETF Content-Usage (robots.txt)", lambda r: bool(r["content_usage_robots"])),
        ("Cloudflare Content-Signal", lambda r: bool(r["content_signal"])),
        ("Training-only token blocked", lambda r: bool(r["training_tokens_blocked"])),
        ("TDMRep reservation", lambda r: r["tdm_reservation"] == 1),
        ("noai / noimageai", lambda r: bool(r["noai"])),
        ("License link (any)", lambda r: bool(r["license"])),
    ]
    out += ["", "## Machine-readable usage signals", "", head, sep]
    for name, test in signals:
        out.append(f"| {name} | " + cols(lambda rs, test=test: f"{sum(test(r) for r in rs)} ({_pct(sum(test(r) for r in rs), len(rs))})") + " |")

    drops = Counter(r["reasons"].split(";")[0].split(" (")[0] for r in rows if r["decision"] == "drop")
    out += [
        "",
        f"## Verdict for `{policy.purpose}` as `{policy.user_agent}`",
        "",
        f"{sum(r['decision'] == 'keep' for r in rows)} kept, {sum(r['decision'] == 'drop' for r in rows)} dropped.",
    ]
    if drops:
        out += [""] + [f"- `{k}`: {v}" for k, v in drops.most_common()]

    out += [
        "",
        "## Per site",
        "",
        "| Site | Category | Blocked | Training tokens | Usage signals | Decision |",
        "|---|---|---:|---|---|---|",
    ]
    for r in rows:
        sig = [
            s
            for s, present in (
                ("Content-Usage", bool(r["content_usage_header"] or r["content_usage_robots"])),
                ("Content-Signal", bool(r["content_signal"])),
                ("TDMRep", r["tdm_reservation"] == 1),
                ("noai", bool(r["noai"])),
            )
            if present
        ]
        blocked = f"{r['ai_crawlers_blocked']}/{r['ai_crawlers_checked']}" if r["ai_crawlers_blocked"] != "" else r["robots_txt"]
        site = r["site"].split("://", 1)[-1].rstrip("/")
        out.append(
            f"| {site} | {r['category']} | {blocked} | {r['training_tokens_blocked'].replace(';', ', ') or '-'} | "
            f"{', '.join(sig) or '-'} | {r['decision']} |"
        )
    out += [
        "",
        "Blocking a crawler by name only says who may fetch a page. Content-Usage, Content-Signal and TDMRep "
        "say how content may be used. A site can be open to every crawler and still opt out of training, "
        "or the other way round.",
    ]
    return "\n".join(out) + "\n"


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
