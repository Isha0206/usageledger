"""Live checks against local HTTP servers that imitate different kinds of sites."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from usageledger import Policy
from usageledger.cli import main
from usageledger.live import PRODUCT_TOKEN, check_url, format_result

CLOUDFLARE_STYLE = """\
# As a condition of accessing this website, you agree to abide by the following content signals ...
User-Agent: *
Content-signal: search=yes, ai-train=no
Allow: /

User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /
"""


class Site:
    """A tiny web server: routes maps path -> (status, [(header, value)], body)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []  # (path, user-agent)
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.requests.append((self.path, self.headers.get("User-Agent")))
                status, headers, body = site.routes.get(self.path, (404, [], "not found"))
                if callable(body):
                    body = body()
                data = body.encode()
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [p for p, _ in self.requests]


HTML = [("Content-Type", "text/html; charset=utf-8")]
PAGE = "<html><head><title>x</title></head><body><p>" + "text " * 100 + "</p></body></html>"


@pytest.fixture()
def sites():
    made = []

    def make(routes):
        s = Site(routes)
        made.append(s)
        return s

    yield make
    for s in made:
        s.server.shutdown()


def test_cloudflare_style_site(sites):
    site = sites({
        "/robots.txt": (200, [("Content-Type", "text/plain")], CLOUDFLARE_STYLE),
        "/article": (200, HTML + [("Content-Usage", "search=y")], PAGE),
    })
    res = check_url(site.url + "/article")
    rec = res.record
    assert res.page_fetched and res.page_status == 200
    assert rec.content_signal["signals"] == {"search": "yes", "ai-train": "no"}
    assert rec.content_signal["prefs"]["train-ai"] == "disallow"
    assert rec.combined["search"] == "allow"  # from the Content-Usage header
    assert not rec.keep and rec.reasons == ["content-signal:ai-train=no (robots.txt)"]
    assert res.crawler_access["GPTBot"] is False and res.crawler_access["CCBot"] is False
    assert res.crawler_access["ClaudeBot"] is True
    # we identified ourselves, and fetched robots.txt before the page
    assert site.paths() == ["/robots.txt", "/article"]
    assert all(ua.startswith(PRODUCT_TOKEN + "/") for _, ua in site.requests)

    as_gpt = check_url(site.url + "/article", Policy(user_agent="GPTBot"))
    assert as_gpt.record.reasons == ["robots:disallow (GPTBot)"]
    assert "Content-Signal  robots.txt: search=yes, ai-train=no" in format_result(res)
    assert "AI crawlers blocked here: GPTBot, CCBot" in format_result(res)


NYT_STYLE = """\
User-agent: *
Disallow: /search

User-agent: GPTBot
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: Applebot-Extended
Disallow: /
"""


def test_training_tokens(sites):
    site = sites({"/robots.txt": (200, [], NYT_STYLE), "/story": (200, HTML, PAGE)})
    res = check_url(site.url + "/story")
    assert res.record.training_tokens == {
        "checked": ["Google-Extended", "Applebot-Extended"],
        "blocked": ["Google-Extended", "Applebot-Extended"],
    }
    assert res.record.reasons == ["training-token:Google-Extended+Applebot-Extended blocked (robots.txt)"]
    assert "Training-only tokens blocked: Google-Extended, Applebot-Extended" in format_result(res)
    assert check_url(site.url + "/story", Policy(honor_training_tokens=False)).record.keep
    # only relevant when deciding about training
    assert check_url(site.url + "/story", Policy(purpose="search")).record.keep


def test_page_not_fetched_when_robots_blocks_us(sites):
    site = sites({
        "/robots.txt": (200, [], "User-agent: usageledger\nDisallow: /\n\nUser-agent: *\nContent-Usage: train-ai=n\n"),
        "/page": (200, HTML, PAGE),
    })
    res = check_url(site.url + "/page")
    assert not res.page_fetched and "does not allow usageledger" in res.skipped_reason
    assert site.paths() == ["/robots.txt"]
    # preferences in robots.txt still count
    assert res.record.reasons == ["aipref:train-ai=n (robots.txt)"]
    assert res.record.content_sha256 is None


def test_unreachable_robots_means_no_page_fetch(sites):
    site = sites({"/robots.txt": (503, [], "busy"), "/page": (200, HTML, PAGE)})
    res = check_url(site.url + "/page")
    assert res.robots_status == 503 and not res.page_fetched
    assert site.paths() == ["/robots.txt"]
    assert res.record.robots["status"] == "unreachable"


def test_missing_robots_and_header_opt_out(sites):
    site = sites({"/page": (200, HTML + [("Content-Usage", "train-ai=n")], PAGE)})
    res = check_url(site.url + "/page")
    assert res.robots_status == 404 and res.record.robots["status"] == "unavailable"
    assert res.page_fetched
    assert res.record.reasons == ["aipref:train-ai=n (http-header)"]
    assert res.crawler_access is not None and all(res.crawler_access.values())  # no robots.txt blocks nothing
    assert len(res.record.content_sha256) == 64


def test_redirect_to_other_origin_uses_its_robots(sites):
    target = sites({
        "/robots.txt": (200, [], "User-agent: *\nContent-Usage: train-ai=y\n"),
        "/final": (200, HTML, PAGE),
    })
    start = sites({"/robots.txt": (200, [], "User-agent: *\nAllow: /\n"), "/go": (302, [("Location", target.url + "/final")], "")})
    res = check_url(start.url + "/go")
    assert res.final_url == target.url + "/final"
    assert res.record.url == target.url + "/final"
    assert res.record.combined["train-ai"] == "allow"
    assert target.paths() == ["/robots.txt", "/final"]


def test_redirect_to_site_that_blocks_us_is_not_followed(sites):
    target = sites({"/robots.txt": (200, [], "User-agent: usageledger\nDisallow: /\n"), "/final": (200, HTML, PAGE)})
    start = sites({"/go": (301, [("Location", target.url + "/final")], "")})
    res = check_url(start.url + "/go")
    assert not res.page_fetched and "does not allow" in res.skipped_reason
    assert target.paths() == ["/robots.txt"]
    assert res.final_url == target.url + "/final"


def test_network_error():
    res = check_url("http://127.0.0.1:9/page")  # discard port, nothing listening
    assert res.robots_status is None and res.robots_error
    assert not res.page_fetched and res.record.robots["status"] == "unreachable"
    assert "not fetched" in format_result(res)


def test_cli_check_reuses_robots_per_site(sites, tmp_path, capsys):
    site = sites({
        "/robots.txt": (200, [], CLOUDFLARE_STYLE),
        "/a": (200, HTML, PAGE),
        "/b": (200, HTML, PAGE),
    })
    out = tmp_path / "live.jsonl"
    rc = main(["check", site.url + "/a", site.url + "/b", "--out", str(out), "--ignore-content-signal"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "2 of 2 kept for train-ai as *." in text
    assert site.paths().count("/robots.txt") == 1
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert recs[0]["live_check"]["crawler_access"]["CCBot"] is False
    assert recs[0]["content_signal"]["signals"]["ai-train"] == "no"


def test_cli_check_no_page(sites, capsys):
    site = sites({"/robots.txt": (200, [], CLOUDFLARE_STYLE), "/a": (200, HTML, PAGE)})
    main(["check", site.url + "/a", "--no-page", "--json"])
    d = json.loads(capsys.readouterr().out)
    assert d["live_check"]["page_fetched"] is False and site.paths() == ["/robots.txt"]
