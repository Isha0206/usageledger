"""Per-document provenance records and keep/drop decisions.

:func:`evaluate` looks at everything known about one fetched page - its HTTP
headers, its HTML, and the robots.txt that was current when it was fetched -
and returns a :class:`ProvenanceRecord`. The record is plain JSON-able data
meant to travel with the document through every later pipeline stage, so the
question "what did the publisher say, and why did we keep this?" can still be
answered at training time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from . import __version__
from .robots import AI_TRAINING_TOKENS, VOCAB_TO_CONTENT_SIGNAL, RobotsLookup, RobotsStore, as_utc
from .signals import HtmlSignals, license_from, noai_from, scan_html, tdmrep_from
from .vocab import DEFAULT_VOCAB, Preference, Vocabulary, combine, parse_statement

__all__ = ["Policy", "ProvenanceRecord", "evaluate", "SCHEMA"]

SCHEMA = "usageledger/provenance/v1"


@dataclass
class Policy:
    """How preferences and signals turn into a keep/drop decision.

    The AIPREF drafts deliberately take no position on what to do with an
    ``unknown`` preference, or on signals outside the vocabulary. Every such
    choice is an explicit field here and is copied into each record.

    Attributes:
        purpose: the usage category the data is collected for, e.g.
            ``"train-ai"`` for pretraining or ``"ai-use"`` for retrieval.
        unknown: ``"keep"`` or ``"drop"`` documents whose preference for
            ``purpose`` is unknown.
        user_agent: robots.txt product token whose group applies. Use the
            token of the crawler that actually fetched the data, e.g.
            ``"CCBot"`` for Common Crawl.
        drop_if_robots_disallowed: drop pages that robots.txt did not allow
            this user agent to crawl at fetch time.
        drop_if_robots_unreachable: drop pages whose robots.txt returned a
            server error (RFC 9309 treats that as "disallow everything").
        honor_content_signal: treat Cloudflare's ``Content-Signal: ai-train=no``
            (and the matching key for other purposes) as an opt-out.
        honor_training_tokens: when ``purpose`` is ``"train-ai"``, treat a
            robots.txt that explicitly blocks a training-only token
            (Google-Extended, Applebot-Extended) as an opt-out.
        honor_tdmrep: treat ``tdm-reservation: 1`` as an opt-out.
        honor_noai: treat ``noai`` in X-Robots-Tag / meta robots as an opt-out.
        exclude_licenses: drop documents whose detected license id contains
            any of these substrings, e.g. ``("-NC-", "-ND-")``.
    """

    purpose: str = "train-ai"
    unknown: str = "keep"
    user_agent: str = "*"
    drop_if_robots_disallowed: bool = True
    drop_if_robots_unreachable: bool = False
    honor_content_signal: bool = True
    honor_training_tokens: bool = True
    honor_tdmrep: bool = True
    honor_noai: bool = False
    exclude_licenses: tuple[str, ...] = ()

    def __post_init__(self):
        if self.unknown not in ("keep", "drop"):
            raise ValueError("Policy.unknown must be 'keep' or 'drop'")
        self.exclude_licenses = tuple(self.exclude_licenses)

    def to_dict(self) -> dict:
        return {
            "purpose": self.purpose,
            "unknown": self.unknown,
            "user_agent": self.user_agent,
            "drop_if_robots_disallowed": self.drop_if_robots_disallowed,
            "drop_if_robots_unreachable": self.drop_if_robots_unreachable,
            "honor_content_signal": self.honor_content_signal,
            "honor_training_tokens": self.honor_training_tokens,
            "honor_tdmrep": self.honor_tdmrep,
            "honor_noai": self.honor_noai,
            "exclude_licenses": list(self.exclude_licenses),
        }


@dataclass
class ProvenanceRecord:
    url: str
    fetched_at: datetime | None
    content_sha256: str | None
    vocab: str
    header: dict | None
    robots: dict | None
    combined: dict[str, str]
    tdmrep: dict
    noai: dict
    license: dict
    keep: bool
    reasons: list[str]
    policy: dict
    doc_id: str | None = None
    est_tokens: int | None = None
    content_signal: dict | None = None
    training_tokens: dict | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def domain(self) -> str:
        from urllib.parse import urlsplit

        return (urlsplit(self.url).hostname or "").lower()

    @property
    def primary_reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "doc_id": self.doc_id,
            "url": self.url,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "content_sha256": self.content_sha256,
            "est_tokens": self.est_tokens,
            "aipref": {
                "vocab": self.vocab,
                "header": self.header,
                "robots": self.robots,
                "combined": self.combined,
            },
            "content_signal": self.content_signal,
            "training_tokens": self.training_tokens,
            "tdmrep": self.tdmrep,
            "noai": self.noai,
            "license": self.license,
            "decision": {"keep": self.keep, "reasons": self.reasons, "policy": self.policy},
            "tool": {"name": "usageledger", "version": __version__},
            "evaluated_at": self.evaluated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProvenanceRecord":
        def dt(v):
            return datetime.fromisoformat(v) if v else None

        a = d.get("aipref", {})
        dec = d.get("decision", {})
        return cls(
            url=d["url"],
            fetched_at=dt(d.get("fetched_at")),
            content_sha256=d.get("content_sha256"),
            vocab=a.get("vocab", ""),
            header=a.get("header"),
            robots=a.get("robots"),
            combined=a.get("combined", {}),
            tdmrep=d.get("tdmrep", {}),
            noai=d.get("noai", {}),
            license=d.get("license", {}),
            keep=dec.get("keep", True),
            reasons=dec.get("reasons", []),
            policy=dec.get("policy", {}),
            doc_id=d.get("doc_id"),
            est_tokens=d.get("est_tokens"),
            content_signal=d.get("content_signal"),
            training_tokens=d.get("training_tokens"),
            evaluated_at=dt(d.get("evaluated_at")) or datetime.now(timezone.utc),
        )


def evaluate(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    html: str | HtmlSignals | None = None,
    fetched_at: datetime | None = None,
    robots: RobotsStore | None = None,
    policy: Policy | None = None,
    vocab: Vocabulary = DEFAULT_VOCAB,
    content_sha256: str | None = None,
    doc_id: str | None = None,
    token_counter: Callable[[str], int] | None = None,
    est_tokens: int | None = None,
) -> ProvenanceRecord:
    """Evaluate one page and return its provenance record.

    Args:
        url: the URL the page was fetched from.
        headers: HTTP response headers, names lower-cased, repeated fields
            already joined with ", ".
        html: the page as HTML (or pre-scanned :class:`HtmlSignals`), used for
            ``<meta>`` opt-outs, license links and a token estimate.
        fetched_at: when the page was fetched; picks the robots.txt snapshot.
        robots: robots.txt snapshots; without it robots.txt is "missing".
        token_counter: optional function text -> token count. The default
            estimate is visible characters / 4.
        est_tokens: a known token count, which skips estimation.
    """
    policy = policy or Policy()
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    fetched_at = as_utc(fetched_at)

    signals: HtmlSignals | None
    if isinstance(html, HtmlSignals):
        signals = html
    elif html:
        signals = scan_html(html, keep_text=est_tokens is None)
    else:
        signals = None

    # AIPREF: the Content-Usage header ...
    header_stmt = None
    if "content-usage" in headers:
        header_stmt = parse_statement(headers["content-usage"], vocab, source="http-header")

    # ... and the robots.txt in force at fetch time.
    lookup: RobotsLookup | None = None
    if robots is not None:
        lookup = robots.lookup(url, fetched_at, policy.user_agent)

    statements = [s for s in (header_stmt, lookup.result.statement if lookup and lookup.result else None) if s]
    combined = combine(statements, vocab)

    content_signal = lookup.result.content_signal if lookup and lookup.result else None
    training_tokens = None
    if lookup is not None and lookup.training_tokens_blocked is not None:
        training_tokens = {"checked": list(AI_TRAINING_TOKENS), "blocked": lookup.training_tokens_blocked}
    tdm = tdmrep_from(headers, signals)
    noai = noai_from(headers, signals)
    lic = license_from(headers, signals)

    # Decision -----------------------------------------------------------
    reasons: list[str] = []
    pref = combined.get(policy.purpose, Preference.UNKNOWN)
    if pref is Preference.DISALLOW:
        where = sorted(s.source for s in statements if s.get(policy.purpose) is Preference.DISALLOW)
        reasons.append(f"aipref:{policy.purpose}=n ({'+'.join(where)})")
    if lookup is not None and lookup.result is not None and not lookup.result.crawlable:
        if lookup.status == "unreachable":
            if policy.drop_if_robots_unreachable:
                reasons.append("robots:unreachable")
        elif policy.drop_if_robots_disallowed:
            reasons.append(f"robots:disallow ({policy.user_agent})")
    if policy.honor_content_signal and content_signal:
        if content_signal["prefs"].get(policy.purpose) == "disallow":
            key = VOCAB_TO_CONTENT_SIGNAL.get(policy.purpose, policy.purpose)
            reasons.append(f"content-signal:{key}=no (robots.txt)")
    if policy.honor_training_tokens and policy.purpose == "train-ai" and training_tokens and training_tokens["blocked"]:
        reasons.append(f"training-token:{'+'.join(training_tokens['blocked'])} blocked (robots.txt)")
    if policy.honor_tdmrep and tdm.reservation == 1:
        reasons.append(f"tdmrep:reserved ({tdm.source})")
    if policy.honor_noai and noai["noai"]:
        reasons.append("noai")
    if lic.spdx and any(x in lic.spdx for x in policy.exclude_licenses):
        reasons.append(f"license:{lic.spdx}")
    cs_allows = bool(
        policy.honor_content_signal and content_signal and content_signal["prefs"].get(policy.purpose) == "allow"
    )
    if not reasons and pref is Preference.UNKNOWN and not cs_allows and policy.unknown == "drop":
        reasons.append(f"aipref:{policy.purpose}=unknown")

    if est_tokens is None and signals is not None:
        if token_counter is not None and signals.text:
            est_tokens = token_counter(signals.text)
        else:
            est_tokens = (signals.text_chars + 3) // 4

    return ProvenanceRecord(
        url=url,
        fetched_at=fetched_at,
        content_sha256=content_sha256,
        vocab=vocab.version,
        header=header_stmt.to_dict() if header_stmt else None,
        robots=lookup.to_dict() if lookup else None,
        combined={k: v.value for k, v in combined.items()},
        tdmrep=tdm.to_dict(),
        noai=noai,
        license=lic.to_dict(),
        keep=not reasons,
        reasons=reasons,
        policy=policy.to_dict(),
        doc_id=doc_id,
        est_tokens=est_tokens,
        content_signal=content_signal,
        training_tokens=training_tokens,
    )


def evaluate_page(page, robots: RobotsStore | None = None, policy: Policy | None = None, **kwargs) -> ProvenanceRecord:
    """Convenience wrapper for a :class:`usageledger.warc.Page`."""
    is_html = page.content_type in ("text/html", "application/xhtml+xml", "")
    return evaluate(
        page.url,
        headers=page.headers,
        html=page.text() if is_html else None,
        fetched_at=page.fetched_at,
        robots=robots,
        policy=policy,
        content_sha256=page.sha256,
        doc_id=page.record_id,
        **kwargs,
    )


def evaluate_many(pages: Iterable, robots: RobotsStore | None = None, policy: Policy | None = None, **kwargs):
    for page in pages:
        yield evaluate_page(page, robots, policy, **kwargs)
