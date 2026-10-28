"""Opt-out and licensing signals other than AIPREF.

These are recorded alongside AIPREF preferences so a pipeline can decide how
to treat them. None of them maps onto the AIPREF vocabulary by specification,
so they are kept as separate, named signals rather than folded into it.

* **TDMRep** (W3C Community Group "TDM Reservation Protocol"): the
  ``tdm-reservation`` / ``tdm-policy`` HTTP headers and the matching HTML
  ``<meta>`` tags. ``tdm-reservation: 1`` means text-and-data-mining rights
  are reserved - the machine-readable opt-out most relevant to EU law.
* **noai / noimageai**: non-standard directives in ``X-Robots-Tag`` or
  ``<meta name="robots">`` used by some platforms.
* **License**: ``rel="license"`` in an HTTP ``Link`` header or in HTML
  ``<link>``/``<a>`` elements. Creative Commons URLs are mapped to SPDX ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = ["HtmlSignals", "scan_html", "TdmRep", "LicenseInfo", "cc_license_id", "parse_link_header"]

# Only the start of a page is scanned for <meta>/<link>; license links in
# footers are caught by scanning the full document for rel="license" anchors.
_MAX_SCAN_CHARS = 2_000_000


@dataclass
class TdmRep:
    reservation: int | None = None  # 1 reserved, 0 not reserved, None not stated
    policy: str | None = None
    source: str | None = None  # "header" | "html"

    def to_dict(self) -> dict:
        return {"reservation": self.reservation, "policy": self.policy, "source": self.source}


@dataclass
class LicenseInfo:
    url: str | None = None
    spdx: str | None = None
    source: str | None = None  # "link-header" | "html"

    def to_dict(self) -> dict:
        return {"url": self.url, "spdx": self.spdx, "source": self.source}


@dataclass
class HtmlSignals:
    meta: dict[str, list[str]] = field(default_factory=dict)  # lower-cased name -> contents
    license_urls: list[str] = field(default_factory=list)
    text_chars: int = 0
    text: str = ""


class _Scanner(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self, keep_text: bool):
        super().__init__(convert_charrefs=True)
        self.out = HtmlSignals()
        self._skip_depth = 0
        self._keep_text = keep_text
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("http-equiv") or "").strip().lower()
            if name:
                self.out.meta.setdefault(name, []).append(a.get("content", "").strip())
        elif tag in ("link", "a"):
            rels = a.get("rel", "").lower().split()
            if "license" in rels and a.get("href"):
                self.out.license_urls.append(a["href"].strip())
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in self._SKIP:
            self._skip_depth -= 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and self._keep_text:
            self._chunks.append(data)


def scan_html(html: str, keep_text: bool = True) -> HtmlSignals:
    """Collect <meta> tags, license links and (optionally) visible text."""
    scanner = _Scanner(keep_text)
    try:
        scanner.feed(html[:_MAX_SCAN_CHARS])
        scanner.close()
    except Exception:  # malformed markup must never break a pipeline
        pass
    out = scanner.out
    if keep_text:
        out.text = re.sub(r"\s+", " ", " ".join(scanner._chunks)).strip()
        out.text_chars = len(out.text)
    return out


def tdmrep_from(headers: dict[str, str], html: HtmlSignals | None) -> TdmRep:
    raw = headers.get("tdm-reservation")
    if raw is not None:
        return TdmRep(_parse_01(raw), headers.get("tdm-policy"), "header")
    if html is not None and "tdm-reservation" in html.meta:
        policy = (html.meta.get("tdm-policy") or [None])[0]
        return TdmRep(_parse_01(html.meta["tdm-reservation"][0]), policy, "html")
    return TdmRep()


def _parse_01(value: str) -> int | None:
    v = value.strip()
    return int(v) if v in ("0", "1") else None


def noai_from(headers: dict[str, str], html: HtmlSignals | None) -> dict:
    """Detect ``noai`` / ``noimageai`` in X-Robots-Tag or meta robots."""
    found: dict[str, str] = {}
    for value in headers.get("x-robots-tag", "").split(","):
        # X-Robots-Tag may be scoped: "otherbot: noai"
        directive = value.strip().lower().rsplit(":", 1)[-1].strip()
        if directive in ("noai", "noimageai"):
            found[directive] = "header"
    if html is not None:
        for content in html.meta.get("robots", []):
            for d in content.lower().replace(" ", "").split(","):
                if d in ("noai", "noimageai"):
                    found.setdefault(d, "html")
    return {"noai": "noai" in found, "noimageai": "noimageai" in found, "sources": found}


_CC_RE = re.compile(
    r"creativecommons\.org/(?:(licenses)/([a-z\-]+)/(\d(?:\.\d)?)|(publicdomain)/(zero|mark)/(\d\.\d))",
    re.IGNORECASE,
)


def cc_license_id(url: str) -> str | None:
    """Map a Creative Commons URL to an SPDX identifier, e.g. CC-BY-NC-4.0."""
    m = _CC_RE.search(url or "")
    if not m:
        return None
    if m.group(1):
        kind, version = m.group(2).lower(), m.group(3)
        if "." not in version:
            version += ".0"
        return f"CC-{kind.upper()}-{version}"
    kind, version = m.group(5).lower(), m.group(6)
    return f"CC0-{version}" if kind == "zero" else "PDM-1.0"


def parse_link_header(value: str) -> list[tuple[str, set[str]]]:
    """Minimal RFC 8288 parser: [(target, {rel, ...}), ...]."""
    links = []
    for m in re.finditer(r"<([^>]*)>\s*((?:;[^,<]*)*)", value or ""):
        target, params = m.group(1), m.group(2)
        rel = re.search(r";\s*rel\s*=\s*\"?([^\";]+)\"?", params, re.IGNORECASE)
        rels = set(rel.group(1).lower().split()) if rel else set()
        links.append((target, rels))
    return links


def license_from(headers: dict[str, str], html: HtmlSignals | None) -> LicenseInfo:
    for target, rels in parse_link_header(headers.get("link", "")):
        if "license" in rels:
            return LicenseInfo(target, cc_license_id(target), "link-header")
    if html is not None and html.license_urls:
        # Prefer a recognisable CC license if several are linked.
        for url in html.license_urls:
            if cc_license_id(url):
                return LicenseInfo(url, cc_license_id(url), "html")
        return LicenseInfo(html.license_urls[0], None, "html")
    return LicenseInfo()
