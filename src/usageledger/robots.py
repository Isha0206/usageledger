"""robots.txt parsing with the AIPREF ``Content-Usage`` rule.

Implements RFC 9309 (groups, user-agent matching, longest-match Allow/Disallow
with ``*`` and ``$``) plus the Content-Usage extension from
draft-ietf-aipref-attach-05, Section 3:

* Content-Usage rules use the same longest-prefix matching as Allow/Disallow.
* A rule without a path applies to every path (with match length 0, so any
  rule with a path that matches is more specific).
* Rules with identical paths are combined, most restrictive first. Unlike
  Allow/Disallow, conflicts do not resolve to the permissive option.
* Preferences only exist for paths the crawler may fetch; for disallowed paths
  there are no preferences at all.

Cloudflare's ``Content-Signal`` rule (``Content-Signal: search=yes,
ai-train=no``), which is common on live sites, is parsed too. It is not part
of AIPREF, so it is reported separately, mapped onto the closest categories.

It also provides :class:`RobotsStore`, which keeps time-stamped robots.txt
snapshots per origin so that a page is judged against the robots.txt that was
current when *it* was fetched (attach-05 Section 3.3: preferences do not apply
retroactively).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator
from urllib.parse import quote, urlsplit

from .vocab import DEFAULT_VOCAB, Statement, Vocabulary, combine, parse_statement, unknown_statement

__all__ = ["RobotsTxt", "RobotsGroup", "RobotsResult", "RobotsSnapshot", "RobotsStore", "origin_of"]

MAX_ROBOTS_BYTES = 500 * 1024  # RFC 9309 Section 2.5
ROBOTS_CACHE_WINDOW = timedelta(hours=24)  # RFC 9309 Section 2.4

_LINE_RE = re.compile(r"^[ \t]*([A-Za-z-]+)[ \t]*:[ \t]*(.*?)[ \t]*$")
_PCT_RE = re.compile(r"%[0-9a-fA-F]{2}")
# Characters RFC 3986 allows unencoded in a path or query, plus the robots
# wildcards. Everything else is percent-encoded before comparison.
_SAFE = "/:@!$&'()*+,;=-._~?%"


def _normalize_path(path: str) -> str:
    """Percent-encode a path the same way for rules and URLs, so that
    ``/caf%C3%A9`` and ``/café`` compare equal and byte lengths are stable."""
    encoded = quote(path, safe=_SAFE)
    return _PCT_RE.sub(lambda m: m.group(0).upper(), encoded)


@dataclass(frozen=True)
class _Pattern:
    raw: str
    regex: re.Pattern
    length: int  # bytes in the encoded pattern, used for longest-match

    @classmethod
    def compile(cls, raw: str) -> "_Pattern":
        norm = _normalize_path(raw)
        anchored = norm.endswith("$")
        body = norm[:-1] if anchored else norm
        rx = ".*".join(re.escape(part) for part in body.split("*"))
        return cls(raw, re.compile(rx + ("$" if anchored else ""), re.DOTALL), len(norm.encode("ascii")))

    def match(self, target: str) -> bool:
        return self.regex.match(target) is not None


@dataclass
class _ContentUsageRule:
    pattern: _Pattern | None  # None = no path, applies to everything
    value: str

    @property
    def length(self) -> int:
        return self.pattern.length if self.pattern else 0

    @property
    def path(self) -> str | None:
        return self.pattern.raw if self.pattern else None


@dataclass
class RobotsGroup:
    user_agents: list[str] = field(default_factory=list)
    allow: list[_Pattern] = field(default_factory=list)
    disallow: list[_Pattern] = field(default_factory=list)
    content_usage: list[_ContentUsageRule] = field(default_factory=list)
    content_signal: list[_ContentUsageRule] = field(default_factory=list)

    def merge(self, other: "RobotsGroup") -> "RobotsGroup":
        return RobotsGroup(
            self.user_agents + other.user_agents,
            self.allow + other.allow,
            self.disallow + other.disallow,
            self.content_usage + other.content_usage,
            self.content_signal + other.content_signal,
        )


@dataclass
class RobotsResult:
    """Outcome of evaluating one URL against one robots.txt."""

    crawlable: bool
    statement: Statement
    group: str  # which user-agent group applied ("*" or a token, "" if none)
    matched_rules: list[dict] = field(default_factory=list)
    content_signal: dict | None = None  # Cloudflare Content-Signal, if any applies

    def to_dict(self) -> dict:
        return {
            "crawlable": self.crawlable,
            "group": self.group,
            "matched_rules": self.matched_rules,
            "statement": self.statement.to_dict(),
        }


def _product_token(ua: str) -> str:
    return re.split(r"[/\s]", ua.strip(), maxsplit=1)[0].lower()


class RobotsTxt:
    """A parsed robots.txt file."""

    def __init__(self, groups: list[RobotsGroup], vocab: Vocabulary = DEFAULT_VOCAB):
        self.groups = groups
        self.vocab = vocab

    @classmethod
    def parse(cls, content: str | bytes, vocab: Vocabulary = DEFAULT_VOCAB) -> "RobotsTxt":
        if isinstance(content, bytes):
            content = content[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace")
        else:
            content = content.encode("utf-8")[:MAX_ROBOTS_BYTES].decode("utf-8", errors="ignore")
        if content.startswith("﻿"):
            content = content[1:]

        groups: list[RobotsGroup] = []
        current: RobotsGroup | None = None
        last_was_agent = False
        for raw_line in re.split(r"\r\n|\r|\n", content):
            line = raw_line.split("#", 1)[0]
            m = _LINE_RE.match(line)
            if not m:
                continue
            key, value = m.group(1).lower(), m.group(2)
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = RobotsGroup()
                    groups.append(current)
                current.user_agents.append(value)
                last_was_agent = True
                continue
            if key not in ("allow", "disallow", "content-usage", "content-signal"):
                continue  # sitemap, crawl-delay, ... do not end a group
            last_was_agent = False
            if current is None:
                continue  # rules before any user-agent line are ignored
            if key == "content-usage":
                current.content_usage.append(_parse_content_usage(value))
            elif key == "content-signal":
                current.content_signal.append(_parse_content_usage(value))
            elif value:  # an empty Allow/Disallow is not a rule
                (current.allow if key == "allow" else current.disallow).append(_Pattern.compile(value))
        return cls(groups, vocab)

    def group_for(self, user_agent: str) -> tuple[RobotsGroup | None, str]:
        token = _product_token(user_agent)
        matches = [g for g in self.groups if any(_product_token(u) == token for u in g.user_agents)]
        label = token
        if not matches and token != "*":
            matches = [g for g in self.groups if any(u.strip() == "*" for u in g.user_agents)]
            label = "*"
        if not matches:
            return None, ""
        merged = matches[0]
        for g in matches[1:]:
            merged = merged.merge(g)
        return merged, label

    def evaluate(self, url_or_path: str, user_agent: str = "*") -> RobotsResult:
        target = _target_of(url_or_path)
        group, label = self.group_for(user_agent)
        if group is None:
            return RobotsResult(True, unknown_statement(self.vocab, source="robots.txt"), "")

        if target == "/robots.txt":
            crawlable = True
        else:
            best_allow = max((p.length for p in group.allow if p.match(target)), default=-1)
            best_disallow = max((p.length for p in group.disallow if p.match(target)), default=-1)
            crawlable = best_disallow < 0 or best_allow >= best_disallow

        if not crawlable:
            return RobotsResult(False, unknown_statement(self.vocab, source="robots.txt"), label)

        signal = _content_signal_for(group.content_signal, target)
        matching = [r for r in group.content_usage if r.pattern is None or r.pattern.match(target)]
        if not matching:
            return RobotsResult(True, unknown_statement(self.vocab, source="robots.txt"), label, [], signal)
        longest = max(r.length for r in matching)
        winners = [r for r in matching if r.length == longest]
        stmts = [parse_statement(r.value, self.vocab, source="robots.txt") for r in winners]
        if len(stmts) == 1:
            statement = stmts[0]
        else:
            statement = Statement(
                combine(stmts, self.vocab),
                vocab=self.vocab,
                raw=" | ".join(r.value for r in winners),
                parse_ok=all(s.parse_ok for s in stmts),
                source="robots.txt",
            )
        rules = [{"path": r.path, "value": r.value} for r in winners]
        return RobotsResult(True, statement, label, rules, signal)


def _parse_content_usage(value: str) -> _ContentUsageRule:
    # attach-05 Section 3.2: a value starting with "/" carries a path, which
    # ends at the first SP or HTAB; the rest is the statement of preference.
    if value.startswith("/"):
        m = re.match(r"(/[^ \t]*)[ \t]*(.*)$", value)
        path, pref = m.group(1), m.group(2)
        return _ContentUsageRule(_Pattern.compile(path), pref.strip(" \t"))
    return _ContentUsageRule(None, value.strip(" \t"))


# Product tokens that never crawl: Google and Apple created them only so a
# site can opt out of AI-model training without leaving search. A robots.txt
# group that names one of them and disallows the URL is therefore an explicit
# AI-training opt-out (for that vendor).
AI_TRAINING_TOKENS = ("Google-Extended", "Applebot-Extended")


def training_tokens_blocked(robots: "RobotsTxt", url: str) -> list[str]:
    """Training-only tokens that robots.txt names explicitly and disallows here."""
    out = []
    for token in AI_TRAINING_TOKENS:
        group, label = robots.group_for(token)
        if group is not None and label != "*" and not robots.evaluate(url, token).crawlable:
            out.append(token)
    return out


# Cloudflare Content-Signal keys -> closest AIPREF categories. The mapping is
# ours, not part of either specification, which is why the signal is kept
# separate from AIPREF preferences in every record.
CONTENT_SIGNAL_TO_VOCAB = {"ai-train": "train-ai", "ai-input": "ai-use", "search": "search"}
VOCAB_TO_CONTENT_SIGNAL = {v: k for k, v in CONTENT_SIGNAL_TO_VOCAB.items()}


def parse_content_signal(value: str) -> dict[str, str]:
    """``search=yes, ai-train=no`` -> {"search": "yes", "ai-train": "no"}."""
    out: dict[str, str] = {}
    for part in value.split(","):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip().lower()] = v.strip().lower()
    return out


def _content_signal_for(rules: list[_ContentUsageRule], target: str) -> dict | None:
    matching = [r for r in rules if r.pattern is None or r.pattern.match(target)]
    if not matching:
        return None
    longest = max(r.length for r in matching)
    winners = [r for r in matching if r.length == longest]
    signals: dict[str, str] = {}
    prefs: dict[str, str] = {}
    for r in winners:
        parsed = parse_content_signal(r.value)
        for k, v in parsed.items():
            # identical paths: most restrictive wins, as for Content-Usage
            if signals.get(k) != "no":
                signals[k] = v
    for cs_key, cat in CONTENT_SIGNAL_TO_VOCAB.items():
        v = signals.get(cs_key)
        prefs[cat] = "allow" if v == "yes" else "disallow" if v == "no" else "unknown"
    return {
        "raw": " | ".join(r.value for r in winners),
        "path": winners[0].path,
        "signals": signals,
        "prefs": prefs,
    }


def _target_of(url_or_path: str) -> str:
    if url_or_path.startswith("/"):
        path, query = url_or_path, ""
        if "?" in path:
            path, query = path.split("?", 1)
    else:
        parts = urlsplit(url_or_path)
        path, query = parts.path or "/", parts.query
    target = path + ("?" + query if query else "")
    return _normalize_path(target)


def origin_of(url: str) -> str:
    """scheme://host[:port], lower-cased, default ports dropped.

    A robots.txt applies to exactly one origin (RFC 9309 Section 2.3)."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


# --------------------------------------------------------------------------
# Time-aware store of robots.txt snapshots
# --------------------------------------------------------------------------


@dataclass
class RobotsSnapshot:
    origin: str
    fetched_at: datetime | None
    status: int  # HTTP status of the robots.txt fetch
    robots: RobotsTxt | None  # None unless status is 2xx
    location: str | None = None  # redirect target for 3xx

    @property
    def kind(self) -> str:
        """RFC 9309 Section 2.3.1: 2xx parse, 4xx "unavailable" (no rules),
        5xx and network errors "unreachable" (assume full disallow)."""
        if 200 <= self.status < 300:
            return "ok"
        if 300 <= self.status < 400:
            return "redirect"
        if 400 <= self.status < 500:
            return "unavailable"
        return "unreachable"


@dataclass
class RobotsLookup:
    """What the store knows about a URL at a given time."""

    status: str  # ok | unavailable | unreachable | missing
    result: RobotsResult | None
    snapshot_fetched_at: datetime | None = None
    time_matched: bool | None = None  # snapshot fetched at or before the page
    stale: bool | None = None  # snapshot older than the 24h cache window
    origin: str = ""
    robots: RobotsTxt | None = None  # the parsed file that applied, if any
    training_tokens_blocked: list[str] | None = None  # None unless a robots.txt was parsed

    def to_dict(self) -> dict:
        d = {
            "origin": self.origin,
            "status": self.status,
            "snapshot_fetched_at": self.snapshot_fetched_at.isoformat() if self.snapshot_fetched_at else None,
            "time_matched": self.time_matched,
            "stale": self.stale,
        }
        if self.result is not None:
            d.update(self.result.to_dict())  # Content-Signal is reported separately
        return d


class RobotsStore:
    """robots.txt snapshots keyed by origin, ordered by fetch time."""

    def __init__(self, vocab: Vocabulary = DEFAULT_VOCAB):
        self.vocab = vocab
        self._snaps: dict[str, list[RobotsSnapshot]] = {}

    def __len__(self) -> int:
        return sum(len(v) for v in self._snaps.values())

    def origins(self) -> Iterator[str]:
        return iter(self._snaps)

    def add(
        self,
        url_or_origin: str,
        content: str | bytes | None,
        fetched_at: datetime | None = None,
        status: int = 200,
        location: str | None = None,
    ) -> RobotsSnapshot:
        origin = origin_of(url_or_origin)
        fetched_at = as_utc(fetched_at)
        robots = RobotsTxt.parse(content or "", self.vocab) if 200 <= status < 300 else None
        snap = RobotsSnapshot(origin, fetched_at, status, robots, location)
        snaps = self._snaps.setdefault(origin, [])
        keys = [_sort_key(s.fetched_at) for s in snaps]
        snaps.insert(bisect.bisect_right(keys, _sort_key(fetched_at)), snap)
        return snap

    def lookup(self, url: str, at: datetime | None = None, user_agent: str = "*") -> RobotsLookup:
        origin = origin_of(url)
        at = as_utc(at)
        snap = self._pick(origin, at)
        if snap is None:
            return RobotsLookup("missing", None, origin=origin)

        # Follow redirects that stay inside the store (RFC 9309: up to 5 hops).
        hops = 0
        while snap.kind == "redirect" and hops < 5 and snap.location:
            nxt = self._pick(origin_of(snap.location), at)
            if nxt is None:
                break
            snap, hops = nxt, hops + 1

        time_matched = None
        stale = None
        if at is not None and snap.fetched_at is not None:
            time_matched = snap.fetched_at <= at
            stale = time_matched and (at - snap.fetched_at) > ROBOTS_CACHE_WINDOW

        kind = snap.kind
        blocked_tokens = None
        if kind == "ok":
            result = snap.robots.evaluate(url, user_agent)
            blocked_tokens = training_tokens_blocked(snap.robots, url)
        elif kind in ("unavailable", "redirect"):
            # No robots.txt in force: everything may be crawled, no preferences.
            kind = "unavailable"
            result = RobotsResult(True, unknown_statement(self.vocab, source="robots.txt"), "")
        else:
            result = RobotsResult(False, unknown_statement(self.vocab, source="robots.txt"), "")
        return RobotsLookup(
            kind, result, snap.fetched_at, time_matched, stale, origin, snap.robots, blocked_tokens
        )

    def snapshot(self, url: str, at: datetime | None = None) -> RobotsSnapshot | None:
        """The snapshot :meth:`lookup` would use for ``url`` at time ``at``."""
        return self._pick(origin_of(url), as_utc(at))

    def _pick(self, origin: str, at: datetime | None) -> RobotsSnapshot | None:
        snaps = self._snaps.get(origin)
        if not snaps:
            return None
        if at is None:
            return snaps[-1]
        dated = [s for s in snaps if s.fetched_at is not None]
        before = [s for s in dated if s.fetched_at <= at]
        if before:
            return before[-1]
        if dated:
            return dated[0]  # closest after; flagged via time_matched=False
        return snaps[-1]

    # Bulk loaders -------------------------------------------------------

    @classmethod
    def from_warc(cls, paths: Iterable[str], vocab: Vocabulary = DEFAULT_VOCAB) -> "RobotsStore":
        """Load robots.txt captures, e.g. Common Crawl's ``robotstxt`` subset."""
        from .warc import iter_responses

        store = cls(vocab)
        for path in paths:
            for page in iter_responses(path):
                if urlsplit(page.url).path != "/robots.txt":
                    continue
                store.add(
                    page.url,
                    page.body,
                    page.fetched_at,
                    page.http_status or 200,
                    page.header("location"),
                )
        return store

    @classmethod
    def from_mapping(
        cls, mapping: dict[str, str], fetched_at: datetime | None = None, vocab: Vocabulary = DEFAULT_VOCAB
    ) -> "RobotsStore":
        store = cls(vocab)
        for origin, text in mapping.items():
            store.add(origin, text, fetched_at)
        return store


def _sort_key(dt: datetime | None) -> float:
    return dt.timestamp() if dt is not None else float("-inf")


def as_utc(dt: datetime | None) -> datetime | None:
    """Treat naive datetimes as UTC so all comparisons are well defined."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
