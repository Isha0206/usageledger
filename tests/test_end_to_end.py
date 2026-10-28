"""End-to-end: synthetic WARCs -> CLI scan / datatrove pipeline -> records and reports."""

import json

import pytest

pytest.importorskip("warcio")

from usageledger.cli import main  # noqa: E402
from usageledger.demo import write_demo_corpus  # noqa: E402

EXPECTED_DROPS = {
    "https://news.example/2026/03/budget": "aipref:train-ai=n (robots.txt)",
    "https://blog.example/post": "aipref:train-ai=n (http-header)",
    "https://shop.example/item": "tdmrep:reserved (header)",
    "https://private.example/members/profile": "robots:disallow (CCBot)",
    "https://changed.example/may": "aipref:train-ai=n (robots.txt)",
    "https://late.example/x": "aipref:train-ai=n (robots.txt)",
    "https://signal.example/x": "content-signal:ai-train=no (robots.txt)",
}


@pytest.fixture()
def corpus(tmp_path):
    return write_demo_corpus(str(tmp_path / "corpus"))


def read_jsonl(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_cli_scan(corpus, tmp_path, capsys):
    pages, robots = corpus
    out, md, js = tmp_path / "prov.jsonl", tmp_path / "r.md", tmp_path / "r.json"
    rc = main(["scan", pages, "--robots", robots, "--user-agent", "CCBot",
               "--out", str(out), "--report", str(md), "--json-report", str(js), "-q"])
    assert rc == 0
    recs = {r["url"]: r for r in read_jsonl(out)}
    assert len(recs) == 18
    dropped = {u: r["decision"]["reasons"][0] for u, r in recs.items() if not r["decision"]["keep"]}
    assert dropped == EXPECTED_DROPS

    assert recs["https://changed.example/april"]["aipref"]["combined"]["train-ai"] == "allow"
    assert recs["https://late.example/x"]["aipref"]["robots"]["time_matched"] is False
    assert recs["https://bad.example/x"]["aipref"]["header"]["parse_ok"] is False
    assert recs["https://art.example/gallery"]["noai"]["noai"] is True
    assert recs["https://nc.example/photo-essay"]["license"]["spdx"] == "CC-BY-NC-4.0"
    assert recs["https://down.example/x"]["aipref"]["robots"]["status"] == "unreachable"
    assert all(len(r["content_sha256"]) == 64 for r in recs.values())

    report = json.loads(js.read_text())
    assert (report["docs"], report["dropped"]) == (18, 7)
    assert "## Why documents were dropped" in md.read_text()


def test_cli_policy_flags(corpus, tmp_path):
    pages, robots = corpus
    out = tmp_path / "p.jsonl"
    main(["scan", pages, "--robots", robots, "--user-agent", "*", "--exclude-license=-NC-",
          "--honor-noai", "--ignore-tdmrep", "--out", str(out), "-q"])
    dropped = {r["url"] for r in read_jsonl(out) if not r["decision"]["keep"]}
    assert "https://ccbot.example/x" in dropped  # the * group opts out
    assert "https://nc.example/photo-essay" in dropped
    assert "https://art.example/gallery" in dropped
    assert "https://shop.example/item" not in dropped


def test_cli_report_merges(corpus, tmp_path):
    pages, robots = corpus
    a, b, merged = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "m.json"
    main(["scan", pages, "--robots", robots, "--json-report", str(a), "-q"])
    main(["scan", pages, "--json-report", str(b), "-q"])
    main(["report", str(a), str(b), "--json-report", str(merged), "-q"])
    assert json.loads(merged.read_text())["docs"] == 36


def test_cli_header_and_robots(tmp_path, capsys):
    main(["header", "train-ai=n, search=y"])
    assert json.loads(capsys.readouterr().out)["prefs"] == {"train-ai": "disallow", "ai-use": "unknown", "search": "allow"}
    rt = tmp_path / "robots.txt"
    rt.write_text("User-agent: *\nContent-Usage: /x/ train-ai=n\n")
    main(["robots", str(rt), "/x/page"])
    assert json.loads(capsys.readouterr().out)["statement"]["prefs"]["train-ai"] == "disallow"


def test_datatrove_pipeline(corpus, tmp_path):
    pytest.importorskip("datatrove")
    pytest.importorskip("cchardet")
    pytest.importorskip("magic")
    from datatrove.executor import LocalPipelineExecutor
    from datatrove.pipeline.writers import JsonlWriter

    from usageledger import Policy
    from usageledger.integrations.datatrove import (
        AuditReportWriter,
        ContentUsageAnnotator,
        ContentUsageFilter,
        ProvenanceWarcReader,
    )

    pages, robots = corpus
    kept_dir, dropped_dir, reports = tmp_path / "kept", tmp_path / "dropped", tmp_path / "reports"
    LocalPipelineExecutor(
        pipeline=[
            ProvenanceWarcReader(str(tmp_path / "corpus"), glob_pattern="pages.warc.gz"),
            ContentUsageAnnotator(robots_warcs=[robots], policy=Policy(user_agent="CCBot")),
            AuditReportWriter(str(reports)),
            ContentUsageFilter(exclusion_writer=JsonlWriter(str(dropped_dir), compression=None)),
            JsonlWriter(str(kept_dir), compression=None),
        ],
        tasks=1,
        workers=1,
        logging_dir=str(tmp_path / "logs"),
    ).run()

    kept = [json.loads(x) for f in kept_dir.glob("*.jsonl") for x in f.read_text().splitlines()]
    dropped = [json.loads(x) for f in dropped_dir.glob("*.jsonl") for x in f.read_text().splitlines()]
    assert len(kept) == 11 and len(dropped) == 7
    assert {d["metadata"]["url"] for d in dropped} == set(EXPECTED_DROPS)
    # the provenance record travels with every kept document
    rec = next(d for d in kept if d["metadata"]["url"] == "https://docs.example/guide")["metadata"]["usageledger"]
    assert rec["aipref"]["header"]["prefs"]["train-ai"] == "allow"
    assert rec["license"]["spdx"] == "CC-BY-4.0"
    assert json.loads((reports / "audit_00000.json").read_text())["dropped"] == 7
