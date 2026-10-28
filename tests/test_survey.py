import csv

from test_live import CLOUDFLARE_STYLE, HTML, PAGE, sites  # noqa: F401  (fixture)

from usageledger.cli import main
from usageledger.survey import posture, read_sites, run_survey, summary_markdown, write_csv

NYT_STYLE = """\
User-agent: GPTBot
Disallow: /

User-agent: ClaudeBot
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: Applebot-Extended
Disallow: /

User-agent: PerplexityBot
Disallow: /
"""


def test_read_sites(tmp_path):
    f = tmp_path / "sites.txt"
    f.write_text("# comment\nwww.a.example  news\n\nhttp://b.example,uni  # trailing\nc.example\n")
    assert read_sites(str(f)) == [
        ("https://www.a.example", "news"),
        ("http://b.example", "uni"),
        ("https://c.example", ""),
    ]


def test_posture():
    assert posture(None) == "unknown"
    assert posture({"a": True, "b": True}) == "blocks none"
    assert posture({"a": False, "b": False}) == "blocks all"
    assert posture({"a": False, "b": True, "c": True}) == "blocks some"
    assert posture({"a": False, "b": False, "c": True}) == "blocks most"


def test_survey_end_to_end(sites, tmp_path, capsys):  # noqa: F811
    news = sites({"/robots.txt": (200, [], NYT_STYLE), "/": (200, HTML, PAGE)})
    cf = sites({"/robots.txt": (200, [], CLOUDFLARE_STYLE), "/": (200, HTML, PAGE)})
    open_site = sites({"/robots.txt": (200, [], "User-agent: *\nDisallow: /admin/\n"), "/": (200, HTML, PAGE)})
    no_robots = sites({"/": (200, HTML + [("Content-Usage", "train-ai=n")], PAGE)})

    rows = run_survey(
        [(news.url + "/", "news"), (cf.url + "/", "news"), (open_site.url + "/", "uni"), (no_robots.url + "/", "uni")],
        delay=0,
    )
    by = {r["category"] + str(i): r for i, r in enumerate(rows)}
    r0, r1, r2, r3 = rows
    assert (r0["posture"], r0["ai_crawlers_blocked"]) == ("blocks most", 6)
    assert r0["training_tokens_blocked"] == "Google-Extended;Applebot-Extended"
    assert r0["allows_GPTBot"] == "no" and r0["allows_Amazonbot"] == "yes"
    assert r0["decision"] == "drop" and r0["reasons"].startswith("training-token:")
    assert r1["content_signal"] == "search=yes, ai-train=no" and r1["blocked"] == "GPTBot;CCBot"
    assert (r2["posture"], r2["decision"]) == ("blocks none", "keep")
    assert r3["robots_txt"] == "not found" and r3["content_usage_header"] == "train-ai=n" and r3["decision"] == "drop"
    assert len(by) == 4

    md = summary_markdown(rows)
    assert "| | All | news | uni |" in md
    assert "| Cloudflare Content-Signal | 1 (25%) | 1 (50%) | 0 (0%) |" in md
    assert "| IETF Content-Usage (HTTP header) | 1 (25%) | 0 (0%) | 1 (50%) |" in md
    assert "| GPTBot | 2 (50%) | 2 (100%) | 0 (0%) |" in md
    assert "2 kept, 2 dropped." not in md  # three are dropped
    assert "1 kept, 3 dropped." in md

    out = tmp_path / "s.csv"
    write_csv(rows, str(out))
    with open(out) as fh:
        assert len(list(csv.DictReader(fh))) == 4

    lst = tmp_path / "sites.txt"
    lst.write_text(f"{news.url}/ news\n{open_site.url}/ uni\n")
    rc = main(["survey", str(lst), "--delay", "0", "--csv", str(tmp_path / "c.csv"), "--report", str(tmp_path / "r.md"), "-q"])
    assert rc == 0
    assert "[2/2]" in capsys.readouterr().err
    assert "## Share of sites blocking each crawler" in (tmp_path / "r.md").read_text()
