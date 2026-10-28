"""A small synthetic crawl that exercises every rule, for tests and demos.

``usageledger demo ./demo`` writes ``pages.warc.gz`` and ``robots.warc.gz``
into ``./demo`` and scans them. All sites use the reserved ``.example`` TLD.
"""

from __future__ import annotations

import os
from io import BytesIO

ROBOTS = [
    # (origin, fetched_at, status, body)
    (
        "https://news.example",
        "2026-03-01T00:00:00Z",
        200,
        "User-Agent: *\nAllow: /\nContent-Usage: train-ai=n\nContent-Usage: /opinion/ train-ai=y\n",
    ),
    (
        "https://private.example",
        "2026-03-01T00:00:00Z",
        200,
        "User-agent: *\nDisallow: /members/\n",
    ),
    (
        "https://ccbot.example",
        "2026-03-01T00:00:00Z",
        200,
        "User-agent: CCBot\nAllow: /\nContent-Usage: train-ai=y\n\nUser-agent: *\nContent-Usage: train-ai=n\n",
    ),
    # Two snapshots: the site opted out in May. A page fetched in April was
    # collected under the old, permissive robots.txt.
    ("https://changed.example", "2026-03-01T00:00:00Z", 200, "User-agent: *\nContent-Usage: train-ai=y\n"),
    ("https://changed.example", "2026-05-01T00:00:00Z", 200, "User-agent: *\nContent-Usage: train-ai=n\n"),
    # Only captured after the page was fetched.
    ("https://late.example", "2026-06-01T00:00:00Z", 200, "User-agent: *\nContent-Usage: train-ai=n\n"),
    # Cloudflare-style Content-Signal (not AIPREF, reported separately).
    (
        "https://signal.example",
        "2026-03-01T00:00:00Z",
        200,
        "User-Agent: *\nContent-signal: search=yes, ai-train=no\nAllow: /\n",
    ),
    ("https://gone.example", "2026-03-01T00:00:00Z", 404, "Not found"),
    ("https://down.example", "2026-03-01T00:00:00Z", 503, "Service unavailable"),
]

_P = "<p>" + "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20 + "</p>"


def _html(title: str, head: str = "", body: str = "") -> str:
    return f"<!doctype html><html><head><title>{title}</title>{head}</head><body><h1>{title}</h1>{_P}{body}</body></html>"


PAGES = [
    # (url, fetched_at, extra headers, html)
    ("https://news.example/2026/03/budget", "2026-03-02T10:00:00Z", [], _html("Budget day")),
    ("https://news.example/opinion/column", "2026-03-02T10:05:00Z", [], _html("A column")),
    ("https://blog.example/post", "2026-03-02T11:00:00Z", [("Content-Usage", "train-ai=n, search=y")], _html("Blog post")),
    (
        "https://docs.example/guide",
        "2026-03-02T12:00:00Z",
        [("Content-Usage", "train-ai=y"), ("Link", '<https://creativecommons.org/licenses/by/4.0/>; rel="license"')],
        _html("Guide"),
    ),
    ("https://shop.example/item", "2026-03-02T13:00:00Z", [("tdm-reservation", "1")], _html("Product")),
    ("https://art.example/gallery", "2026-03-02T14:00:00Z", [], _html("Gallery", '<meta name="robots" content="noai, noimageai">')),
    (
        "https://wiki.example/page",
        "2026-03-02T15:00:00Z",
        [],
        _html("Wiki", body='<a rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA</a>'),
    ),
    (
        "https://nc.example/photo-essay",
        "2026-03-02T15:30:00Z",
        [],
        _html("Essay", '<link rel="license" href="https://creativecommons.org/licenses/by-nc/4.0/">'),
    ),
    ("https://private.example/members/profile", "2026-03-02T16:00:00Z", [], _html("Members only")),
    ("https://bad.example/x", "2026-03-02T17:00:00Z", [("Content-Usage", "Train-AI=n")], _html("Typo in header")),
    ("https://ccbot.example/x", "2026-03-02T18:00:00Z", [], _html("Bot-specific rules")),
    ("https://changed.example/april", "2026-04-10T09:00:00Z", [], _html("Before the opt-out")),
    ("https://changed.example/may", "2026-05-10T09:00:00Z", [], _html("After the opt-out")),
    ("https://late.example/x", "2026-04-01T09:00:00Z", [], _html("Robots captured later")),
    ("https://signal.example/x", "2026-03-02T19:00:00Z", [], _html("Cloudflare-style signal")),
    ("https://gone.example/x", "2026-03-03T09:00:00Z", [], _html("No robots.txt")),
    ("https://down.example/x", "2026-03-03T09:00:00Z", [], _html("Robots server error")),
    ("https://plain.example/x", "2026-03-03T09:00:00Z", [], _html("Nothing stated anywhere")),
]


def _write(path: str, records) -> None:
    try:
        from warcio.statusandheaders import StatusAndHeaders
        from warcio.warcwriter import WARCWriter
    except ImportError as exc:
        raise ImportError("The demo writes WARC files, which needs warcio: pip install 'usageledger[warc]'") from exc

    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for url, date, status, headers, body in records:
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}.get(status, "OK")
            http = StatusAndHeaders(f"{status} {reason}", headers, protocol="HTTP/1.1")
            rec = writer.create_warc_record(
                url,
                "response",
                payload=BytesIO(body.encode("utf-8")),
                http_headers=http,
                warc_headers_dict={"WARC-Date": date, "WARC-Identified-Payload-Type": "text/html"},
            )
            writer.write_record(rec)


def write_demo_corpus(directory: str) -> tuple[str, str]:
    """Write ``pages.warc.gz`` and ``robots.warc.gz``; return their paths."""
    os.makedirs(directory, exist_ok=True)
    pages_path = os.path.join(directory, "pages.warc.gz")
    robots_path = os.path.join(directory, "robots.warc.gz")
    _write(
        pages_path,
        [(u, d, 200, [("Content-Type", "text/html; charset=utf-8"), *h], html) for u, d, h, html in PAGES],
    )
    _write(
        robots_path,
        [(o + "/robots.txt", d, s, [("Content-Type", "text/plain")], body) for o, d, s, body in ROBOTS],
    )
    return pages_path, robots_path
