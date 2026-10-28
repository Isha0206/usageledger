from datetime import datetime, timezone

import pytest

from usageledger import Preference, RobotsStore, RobotsTxt, origin_of

A, D, U = Preference.ALLOW, Preference.DISALLOW, Preference.UNKNOWN

# draft-ietf-aipref-attach-05, Figure 2
SPEC_EXAMPLE = """\
User-Agent: *
Allow: /
Disallow: /never/
Content-Usage: train-ai=n
Content-Usage: /ai-ok/ train-ai=y

User-Agent: ExampleBot
Allow: /
Content-Usage: train-ai=y
"""


@pytest.mark.parametrize(
    "path, crawlable, pref",
    [("/test", True, D), ("/never/test", False, U), ("/ai-ok/test", True, A)],
)
def test_spec_table_1(path, crawlable, pref):
    r = RobotsTxt.parse(SPEC_EXAMPLE).evaluate(path, "SomeBot")
    assert r.crawlable is crawlable
    assert r.statement.get("train-ai") is pref
    assert r.group == "*"


def test_specific_group_wins_and_is_case_insensitive():
    robots = RobotsTxt.parse(SPEC_EXAMPLE)
    for path in ("/test", "/never/test", "/ai-ok/x"):
        r = robots.evaluate(path, "examplebot/2.1")
        assert r.crawlable and r.statement.get("train-ai") is A and r.group == "examplebot"


def test_identical_paths_combine_most_restrictive():
    robots = RobotsTxt.parse("User-agent: *\nContent-Usage: /a/ train-ai=y\nContent-Usage: /a/ train-ai=n, search=y\n")
    r = robots.evaluate("/a/page")
    assert r.statement.get("train-ai") is D
    assert r.statement.get("search") is A
    assert len(r.matched_rules) == 2


def test_rules_are_order_independent_and_interleaved():
    robots = RobotsTxt.parse("User-agent: *\nContent-Usage: /x/ train-ai=y\nDisallow: /y/\nContent-Usage: train-ai=n\n")
    assert robots.evaluate("/x/1").statement.get("train-ai") is A
    assert robots.evaluate("/z").statement.get("train-ai") is D
    assert not robots.evaluate("/y/1").crawlable


def test_wildcards_and_end_anchor():
    robots = RobotsTxt.parse(
        "User-agent: *\nContent-Usage: /*.pdf$ train-ai=n\nContent-Usage: /docs/* train-ai=y\nDisallow: /*?session=\n"
    )
    assert robots.evaluate("/docs/a.html").statement.get("train-ai") is A
    assert robots.evaluate("/files/a.pdf").statement.get("train-ai") is D
    assert robots.evaluate("/files/a.pdf?x=1").statement.get("train-ai") is U  # $ anchors the end
    assert not robots.evaluate("/page?session=1").crawlable


def test_longest_match_allow_beats_disallow_on_tie():
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /a\nAllow: /a\nDisallow: /b/\nAllow: /b/c\n")
    assert robots.evaluate("/a").crawlable
    assert robots.evaluate("/b/c/d").crawlable
    assert not robots.evaluate("/b/x").crawlable


def test_comments_tabs_and_whitespace():
    robots = RobotsTxt.parse("User-agent: *   # all bots\n  content-usage :\t/p/\ttrain-ai=n   # opt out\n")
    r = robots.evaluate("/p/1")
    assert r.statement.get("train-ai") is D
    assert r.matched_rules == [{"path": "/p/", "value": "train-ai=n"}]


def test_groups_merge_and_rules_before_user_agent_are_ignored():
    robots = RobotsTxt.parse(
        "Content-Usage: train-ai=y\nUser-agent: a\nUser-agent: b\nContent-Usage: train-ai=n\n"
        "Sitemap: https://x/s.xml\nUser-agent: a\nContent-Usage: /p/ search=n\n"
    )
    r = robots.evaluate("/p/1", "a")
    assert r.statement.get("search") is D  # second "a" group merged in
    assert robots.evaluate("/q", "b").statement.get("train-ai") is D
    assert robots.evaluate("/q", "other").statement.get("train-ai") is U  # no * group


def test_robots_txt_itself_is_always_crawlable_and_bad_values_are_unknown():
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /\nContent-Usage: TRAIN-AI=n\n")
    assert robots.evaluate("/robots.txt").crawlable
    r = RobotsTxt.parse("User-agent: *\nContent-Usage: TRAIN-AI=n\n").evaluate("/x")
    assert not r.statement.parse_ok and r.statement.is_empty


def test_percent_encoding_is_normalised():
    robots = RobotsTxt.parse("User-agent: *\nContent-Usage: /caf%c3%a9/ train-ai=n\n")
    assert robots.evaluate("https://x.example/café/menu").statement.get("train-ai") is D


def test_origin_of():
    assert origin_of("HTTPS://Example.COM:443/a") == "https://example.com"
    assert origin_of("http://example.com:8080/a") == "http://example.com:8080"


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_store_uses_snapshot_current_at_fetch_time():
    store = RobotsStore()
    store.add("https://s.example", "User-agent: *\nContent-Usage: train-ai=n\n", dt("2026-05-01"))
    store.add("https://s.example", "User-agent: *\nContent-Usage: train-ai=y\n", dt("2026-03-01"))

    before = store.lookup("https://s.example/p", dt("2026-04-10"))
    assert before.result.statement.get("train-ai") is A and before.time_matched and before.stale

    after = store.lookup("https://s.example/p", dt("2026-05-01T12:00"))
    assert after.result.statement.get("train-ai") is D and after.time_matched and not after.stale

    early = store.lookup("https://s.example/p", dt("2026-01-01"))
    assert early.time_matched is False and early.result.statement.get("train-ai") is A


def test_store_status_handling():
    store = RobotsStore()
    store.add("https://gone.example", "nope", dt("2026-03-01"), status=404)
    store.add("https://down.example", "err", dt("2026-03-01"), status=503)
    store.add("http://moved.example", "", dt("2026-03-01"), status=301, location="https://moved.example/robots.txt")
    store.add("https://moved.example", "User-agent: *\nContent-Usage: train-ai=n\n", dt("2026-03-01"))

    gone = store.lookup("https://gone.example/x", dt("2026-03-02"))
    assert gone.status == "unavailable" and gone.result.crawlable
    down = store.lookup("https://down.example/x", dt("2026-03-02"))
    assert down.status == "unreachable" and not down.result.crawlable
    moved = store.lookup("http://moved.example/x", dt("2026-03-02"))
    assert moved.status == "ok" and moved.result.statement.get("train-ai") is D
    assert store.lookup("https://nowhere.example/x").status == "missing"


def test_naive_datetimes_are_utc():
    store = RobotsStore()
    store.add("https://n.example", "User-agent: *\nContent-Usage: train-ai=n\n", datetime(2026, 3, 1))
    assert store.lookup("https://n.example/x", dt("2026-03-01T01:00")).time_matched


def test_training_tokens_must_be_named_explicitly():
    from usageledger.robots import training_tokens_blocked

    everything_blocked = RobotsTxt.parse("User-agent: *\nDisallow: /\n")
    assert training_tokens_blocked(everything_blocked, "/a") == []  # a general block is not an AI opt-out
    partial = RobotsTxt.parse("User-agent: Google-Extended\nDisallow: /private/\n")
    assert training_tokens_blocked(partial, "/private/x") == ["Google-Extended"]
    assert training_tokens_blocked(partial, "/public/x") == []
    store = RobotsStore()
    store.add("https://t.example", "User-agent: applebot-extended\nDisallow: /\n")
    assert store.lookup("https://t.example/a").training_tokens_blocked == ["Applebot-Extended"]
    assert store.lookup("https://nowhere.example/a").training_tokens_blocked is None
