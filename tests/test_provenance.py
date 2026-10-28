import json
from datetime import datetime, timezone

import pytest

from usageledger import AuditReport, Policy, ProvenanceRecord, RobotsStore, evaluate
from usageledger.signals import cc_license_id, parse_link_header, scan_html

T = datetime(2026, 3, 2, tzinfo=timezone.utc)
HTML = "<html><head><title>t</title></head><body><p>" + "word " * 400 + "</p></body></html>"


def store(text, origin="https://s.example"):
    s = RobotsStore()
    s.add(origin, text, datetime(2026, 3, 1, tzinfo=timezone.utc))
    return s


def test_header_disallow_drops():
    rec = evaluate("https://s.example/a", headers={"Content-Usage": "train-ai=n"}, html=HTML, fetched_at=T)
    assert not rec.keep
    assert rec.reasons == ["aipref:train-ai=n (http-header)"]
    assert rec.combined["train-ai"] == "disallow"


def test_header_allow_but_robots_disallow_is_most_restrictive():
    rec = evaluate(
        "https://s.example/a",
        headers={"content-usage": "train-ai=y"},
        fetched_at=T,
        robots=store("User-agent: *\nContent-Usage: train-ai=n\n"),
    )
    assert not rec.keep and rec.reasons == ["aipref:train-ai=n (robots.txt)"]


def test_both_sources_named():
    rec = evaluate(
        "https://s.example/a",
        headers={"content-usage": "train-ai=n"},
        fetched_at=T,
        robots=store("User-agent: *\nContent-Usage: train-ai=n\n"),
    )
    assert rec.reasons == ["aipref:train-ai=n (http-header+robots.txt)"]


def test_purpose_selects_category():
    hdr = {"content-usage": "train-ai=n, ai-use=y"}
    assert not evaluate("https://x/a", headers=hdr).keep
    assert evaluate("https://x/a", headers=hdr, policy=Policy(purpose="ai-use")).keep


def test_unknown_policy():
    assert evaluate("https://x/a").keep
    rec = evaluate("https://x/a", policy=Policy(unknown="drop"))
    assert not rec.keep and rec.reasons == ["aipref:train-ai=unknown"]


def test_robots_disallow_and_unreachable():
    s = store("User-agent: *\nDisallow: /private/\n")
    assert evaluate("https://s.example/private/a", fetched_at=T, robots=s).reasons == ["robots:disallow (*)"]
    assert evaluate(
        "https://s.example/private/a", fetched_at=T, robots=s, policy=Policy(drop_if_robots_disallowed=False)
    ).keep

    down = RobotsStore()
    down.add("https://d.example", "", T, status=500)
    assert evaluate("https://d.example/a", fetched_at=T, robots=down).keep
    assert not evaluate(
        "https://d.example/a", fetched_at=T, robots=down, policy=Policy(drop_if_robots_unreachable=True)
    ).keep


def test_user_agent_group():
    s = store("User-agent: CCBot\nContent-Usage: train-ai=y\n\nUser-agent: *\nContent-Usage: train-ai=n\n")
    assert evaluate("https://s.example/a", fetched_at=T, robots=s, policy=Policy(user_agent="CCBot")).keep
    assert not evaluate("https://s.example/a", fetched_at=T, robots=s).keep


def test_tdmrep_header_and_meta():
    rec = evaluate("https://x/a", headers={"tdm-reservation": "1", "tdm-policy": "https://x/p.json"})
    assert not rec.keep and rec.tdmrep == {"reservation": 1, "policy": "https://x/p.json", "source": "header"}
    meta = '<html><head><meta name="tdm-reservation" content="1"></head></html>'
    assert evaluate("https://x/a", html=meta).reasons == ["tdmrep:reserved (html)"]
    assert evaluate("https://x/a", html=meta, policy=Policy(honor_tdmrep=False)).keep
    assert evaluate("https://x/a", headers={"tdm-reservation": "0"}).keep


def test_noai():
    html = '<html><head><meta name="robots" content="index, noai"></head></html>'
    rec = evaluate("https://x/a", html=html)
    assert rec.keep and rec.noai["noai"] and rec.noai["sources"] == {"noai": "html"}
    assert not evaluate("https://x/a", html=html, policy=Policy(honor_noai=True)).keep
    hdr = evaluate("https://x/a", headers={"x-robots-tag": "otherbot: noimageai"})
    assert hdr.noai["noimageai"] and not hdr.noai["noai"]


def test_license_detection_and_exclusion():
    html = '<html><body><a rel="nofollow license" href="https://creativecommons.org/licenses/by-nc-nd/4.0/">x</a></body></html>'
    rec = evaluate("https://x/a", html=html)
    assert rec.license == {
        "url": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
        "spdx": "CC-BY-NC-ND-4.0",
        "source": "html",
    }
    assert evaluate("https://x/a", html=html, policy=Policy(exclude_licenses=("-NC-",))).reasons == [
        "license:CC-BY-NC-ND-4.0"
    ]
    hdr = evaluate("https://x/a", headers={"link": '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'})
    assert hdr.license["spdx"] == "CC-BY-4.0" and hdr.license["source"] == "link-header"


@pytest.mark.parametrize(
    "url, spdx",
    [
        ("https://creativecommons.org/licenses/by-sa/3.0/de/", "CC-BY-SA-3.0"),
        ("http://creativecommons.org/licenses/by/2/", "CC-BY-2.0"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        ("https://creativecommons.org/publicdomain/mark/1.0/", "PDM-1.0"),
        ("https://example.com/license", None),
    ],
)
def test_cc_license_id(url, spdx):
    assert cc_license_id(url) == spdx


def test_link_header_parser():
    links = parse_link_header('<https://a/>; rel="license", <https://b/>; rel=next')
    assert links == [("https://a/", {"license"}), ("https://b/", {"next"})]


def test_visible_text_excludes_scripts():
    s = scan_html("<html><head><script>var x = 'hidden';</script><style>p{}</style></head><body>Hello <b>world</b></body></html>")
    assert s.text == "Hello world"


def test_token_estimate_and_custom_counter():
    rec = evaluate("https://x/a", html=HTML)
    assert rec.est_tokens == (len(("word " * 400).strip()) + len("t ") + 3) // 4
    assert evaluate("https://x/a", html=HTML, token_counter=lambda t: len(t.split())).est_tokens == 401
    assert evaluate("https://x/a", html=HTML, est_tokens=7).est_tokens == 7


def test_record_round_trip_is_json_safe():
    rec = evaluate(
        "https://s.example/a",
        headers={"content-usage": "train-ai=n"},
        html=HTML,
        fetched_at=T,
        robots=store("User-agent: *\nContent-Usage: search=y\n"),
        content_sha256="abc",
        doc_id="doc-1",
    )
    d = json.loads(json.dumps(rec.to_dict()))
    assert d["schema"] == "usageledger/provenance/v1"
    assert d["aipref"]["vocab"] == "draft-ietf-aipref-vocab-08"
    assert d["aipref"]["robots"]["statement"]["prefs"]["search"] == "allow"
    back = ProvenanceRecord.from_dict(d)
    assert back.to_dict() == d


def test_audit_report_and_merge():
    recs = [
        evaluate("https://a.example/1", headers={"content-usage": "train-ai=n"}, est_tokens=100),
        evaluate("https://b.example/1", headers={"content-usage": "train-ai=y"}, est_tokens=50),
        evaluate("https://c.example/1", headers={"content-usage": "Bad=1"}, est_tokens=10),
    ]
    one = AuditReport().extend(recs[:2])
    two = AuditReport().extend(recs[2:])
    merged = one.merge(two)
    assert (merged.docs, merged.kept, merged.dropped) == (3, 2, 1)
    assert (merged.tokens_kept, merged.tokens_dropped) == (60, 100)
    assert merged.reasons == {"aipref:train-ai=n (http-header)": 1}
    assert merged.header_parse_errors == 1
    assert merged.sources["http-header"] == 2
    assert merged.combined["train-ai"] == {"disallow": 1, "allow": 1, "unknown": 1}
    assert AuditReport.from_dict(json.loads(merged.to_json())).to_dict() == merged.to_dict()
    md = merged.to_markdown()
    assert "aipref:train-ai=n (http-header)" in md and "| Dropped | 1 (33.3%)" in md
