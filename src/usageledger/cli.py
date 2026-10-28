"""Command line interface.

    usageledger header "train-ai=n, search=y"
    usageledger robots robots.txt /some/path --user-agent CCBot
    usageledger scan crawl.warc.gz --robots robotstxt.warc.gz --user-agent CCBot \\
        --out provenance.jsonl --report report.md
    usageledger report provenance.jsonl [more.jsonl ...] --report report.md
    usageledger check https://example.com/some/page [more URLs ...]
    usageledger survey sites.txt --csv survey.csv --report survey.md
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .audit import AuditReport
from .provenance import Policy, evaluate_page
from .robots import RobotsStore, RobotsTxt
from .vocab import parse_statement
from .warc import iter_responses


def _cmd_header(args) -> int:
    stmt = parse_statement(args.value, source="http-header")
    print(json.dumps(stmt.to_dict(), indent=2))
    return 0


def _cmd_robots(args) -> int:
    with open(args.robots_file, "rb") as fh:
        robots = RobotsTxt.parse(fh.read())
    result = robots.evaluate(args.path, args.user_agent)
    print(json.dumps({**result.to_dict(), "content_signal": result.content_signal}, indent=2))
    return 0


def _policy_from(args) -> Policy:
    excl = tuple(x.strip() for x in (args.exclude_license or "").split(",") if x.strip())
    return Policy(
        purpose=args.purpose,
        unknown=args.unknown,
        user_agent=args.user_agent,
        drop_if_robots_disallowed=not args.keep_robots_disallowed,
        drop_if_robots_unreachable=args.drop_robots_unreachable,
        honor_content_signal=not args.ignore_content_signal,
        honor_training_tokens=not args.ignore_training_tokens,
        honor_tdmrep=not args.ignore_tdmrep,
        honor_noai=args.honor_noai,
        exclude_licenses=excl,
    )


def _cmd_scan(args) -> int:
    policy = _policy_from(args)
    robots = RobotsStore.from_warc(args.robots) if args.robots else None
    if robots is not None:
        print(f"Loaded {len(robots):,} robots.txt snapshots", file=sys.stderr)

    report = AuditReport()
    out = open(args.out, "w", encoding="utf-8") if args.out else None
    try:
        for path in args.warc:
            for page in iter_responses(path):
                if page.url.endswith("/robots.txt"):
                    continue
                if not args.all_types and page.content_type not in ("text/html", "application/xhtml+xml"):
                    continue
                rec = evaluate_page(page, robots, policy)
                report.add(rec)
                if out:
                    out.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
    finally:
        if out:
            out.close()

    _write_reports(report, args)
    return 0


def _cmd_check(args) -> int:
    from .live import check_url, format_result

    policy = _policy_from(args)
    store, cache = RobotsStore(), {}
    report = AuditReport()
    out = open(args.out, "w", encoding="utf-8") if args.out else None
    try:
        for i, url in enumerate(args.urls):
            if "://" not in url:
                url = "https://" + url
            res = check_url(url, policy, store=store, fetch_page=not args.no_page, _cache=cache)
            report.add(res.record)
            if out:
                out.write(json.dumps(res.to_dict(), ensure_ascii=False) + "\n")
            if args.json:
                print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(("\n" if i else "") + format_result(res))
    finally:
        if out:
            out.close()
    if len(args.urls) > 1 and not args.json:
        print(f"\n{report.kept} of {report.docs} kept for {policy.purpose} as {policy.user_agent}.")
    return 0


def _cmd_survey(args) -> int:
    from .survey import _stderr, read_sites, run_survey, summary_markdown, write_csv

    policy = _policy_from(args)
    sites = read_sites(args.sites)
    if not sites:
        print(f"No sites found in {args.sites}", file=sys.stderr)
        return 1
    _stderr(f"Checking {len(sites)} sites, one at a time, {args.delay:g}s apart ...")
    rows = run_survey(sites, policy, fetch_page=not args.no_page, delay=args.delay, progress=_stderr)
    if args.csv:
        write_csv(rows, args.csv)
    md = summary_markdown(rows, policy)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(md)
    if not args.quiet:
        print(md)
    return 0


def _cmd_demo(args) -> int:
    import os

    from .demo import write_demo_corpus

    pages, robots = write_demo_corpus(args.directory)
    print(f"Wrote {pages} and {robots}\n", file=sys.stderr)
    return main(
        [
            "scan", pages,
            "--robots", robots,
            "--user-agent", args.user_agent,
            "--out", os.path.join(args.directory, "provenance.jsonl"),
            "--report", os.path.join(args.directory, "report.md"),
            "--json-report", os.path.join(args.directory, "report.json"),
        ]
    )


def _cmd_report(args) -> int:
    report = AuditReport()
    for path in args.jsonl:
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as fh:
                report = report.merge(AuditReport.from_dict(json.load(fh)))
        else:
            report = report.merge(AuditReport.from_jsonl(path))
    _write_reports(report, args)
    return 0


def _write_reports(report: AuditReport, args) -> None:
    if args.json_report:
        report.to_json(args.json_report)
    md = report.to_markdown()
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(md)
    if not args.quiet:
        print(md)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="usageledger", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"usageledger {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("header", help="parse a Content-Usage value")
    h.add_argument("value")
    h.set_defaults(func=_cmd_header)

    r = sub.add_parser("robots", help="evaluate a path against a robots.txt file")
    r.add_argument("robots_file")
    r.add_argument("path", help="URL or path, e.g. /blog/post")
    r.add_argument("--user-agent", default="*")
    r.set_defaults(func=_cmd_robots)

    def report_opts(sp):
        sp.add_argument("--report", help="write the Markdown audit report here")
        sp.add_argument("--json-report", help="write the JSON audit report here (mergeable)")
        sp.add_argument("-q", "--quiet", action="store_true", help="do not print the report")

    def policy_opts(sp):
        sp.add_argument("--purpose", default="train-ai", help="usage category to decide on (default train-ai)")
        sp.add_argument("--unknown", choices=("keep", "drop"), default="keep", help="what to do when unknown")
        sp.add_argument("--user-agent", default="*", help="robots.txt group to apply, e.g. CCBot")
        sp.add_argument("--keep-robots-disallowed", action="store_true")
        sp.add_argument("--drop-robots-unreachable", action="store_true")
        sp.add_argument("--ignore-content-signal", action="store_true", help="ignore Cloudflare Content-Signal")
        sp.add_argument(
            "--ignore-training-tokens", action="store_true", help="ignore blocked Google-Extended / Applebot-Extended"
        )
        sp.add_argument("--ignore-tdmrep", action="store_true")
        sp.add_argument("--honor-noai", action="store_true")
        sp.add_argument("--exclude-license", help="comma-separated substrings, e.g. --exclude-license=-NC-,-ND-")

    s = sub.add_parser("scan", help="evaluate every page in WARC files")
    s.add_argument("warc", nargs="+", help="WARC files with the pages (e.g. Common Crawl WARC)")
    s.add_argument("--robots", nargs="*", help="WARC files with robots.txt captures")
    s.add_argument("--out", help="write one provenance record per page (JSON Lines)")
    policy_opts(s)
    s.add_argument("--all-types", action="store_true", help="include non-HTML responses")
    report_opts(s)
    s.set_defaults(func=_cmd_scan)

    c = sub.add_parser("check", help="check live URLs: fetch robots.txt and the page now")
    c.add_argument("urls", nargs="+", help="URLs to check (https:// is added if missing)")
    c.add_argument("--no-page", action="store_true", help="only fetch robots.txt, not the pages")
    c.add_argument("--out", help="also write one record per URL (JSON Lines)")
    c.add_argument("--json", action="store_true", help="print full JSON records instead of summaries")
    policy_opts(c)
    c.set_defaults(func=_cmd_check)

    v = sub.add_parser("survey", help="check a list of sites and summarise their AI opt-out signals")
    v.add_argument("sites", help="text file: one site per line, optional category after it")
    v.add_argument("--csv", help="write one row per site here")
    v.add_argument("--report", help="write the Markdown summary here")
    v.add_argument("--delay", type=float, default=1.0, help="seconds to wait between sites (default 1)")
    v.add_argument("--no-page", action="store_true", help="only fetch robots.txt, not the pages")
    v.add_argument("-q", "--quiet", action="store_true", help="do not print the summary")
    policy_opts(v)
    v.set_defaults(func=_cmd_survey)

    d = sub.add_parser("demo", help="write a small synthetic crawl and scan it")
    d.add_argument("directory", nargs="?", default="usageledger-demo")
    d.add_argument("--user-agent", default="CCBot")
    d.set_defaults(func=_cmd_demo)

    m = sub.add_parser("report", help="build or merge reports from provenance JSONL / JSON reports")
    m.add_argument("jsonl", nargs="+")
    report_opts(m)
    m.set_defaults(func=_cmd_report)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ImportError as exc:  # an optional extra is not installed
        print(f"usageledger: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
