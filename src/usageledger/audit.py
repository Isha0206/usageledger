"""Aggregate provenance records into an audit report.

The report answers the questions a data team gets asked after the fact: how
much was dropped, for which reason, from which sites, how many tokens that
cost, and how much of the corpus had any machine-readable preference at all.
Reports from parallel workers can be merged.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from . import __version__
from .provenance import ProvenanceRecord

__all__ = ["AuditReport"]


@dataclass
class AuditReport:
    docs: int = 0
    kept: int = 0
    dropped: int = 0
    tokens_kept: int = 0
    tokens_dropped: int = 0
    reasons: Counter = field(default_factory=Counter)  # reason kind -> docs
    reason_tokens: Counter = field(default_factory=Counter)
    dropped_domains: Counter = field(default_factory=Counter)
    # category -> preference -> docs, after combining all sources
    combined: dict[str, Counter] = field(default_factory=dict)
    # where explicit preferences came from
    sources: Counter = field(default_factory=Counter)
    robots_status: Counter = field(default_factory=Counter)
    robots_time_mismatch: int = 0
    header_parse_errors: int = 0
    robots_parse_errors: int = 0
    tdmrep_reserved: int = 0
    noai: int = 0
    content_signal_docs: int = 0
    training_token_docs: int = 0
    training_tokens: Counter = field(default_factory=Counter)  # token -> docs where blocked
    content_signals: Counter = field(default_factory=Counter)  # "ai-train=no" -> docs
    licenses: Counter = field(default_factory=Counter)
    policy: dict | None = None
    vocab: str | None = None

    # Building ----------------------------------------------------------

    def add(self, rec: ProvenanceRecord) -> None:
        self.docs += 1
        tokens = rec.est_tokens or 0
        if rec.keep:
            self.kept += 1
            self.tokens_kept += tokens
        else:
            self.dropped += 1
            self.tokens_dropped += tokens
            kind = _reason_kind(rec.primary_reason)
            self.reasons[kind] += 1
            self.reason_tokens[kind] += tokens
            self.dropped_domains[rec.domain] += 1

        for cat, pref in rec.combined.items():
            self.combined.setdefault(cat, Counter())[pref] += 1

        header = rec.header or {}
        if header:
            if not header.get("parse_ok", True):
                self.header_parse_errors += 1
            elif any(v != "unknown" for v in header.get("prefs", {}).values()):
                self.sources["http-header"] += 1

        robots = rec.robots
        if robots is None:
            self.robots_status["not checked"] += 1
        else:
            self.robots_status[robots.get("status", "?")] += 1
            if robots.get("time_matched") is False:
                self.robots_time_mismatch += 1
            stmt = robots.get("statement") or {}
            if stmt and not stmt.get("parse_ok", True):
                self.robots_parse_errors += 1
            elif any(v != "unknown" for v in stmt.get("prefs", {}).values()):
                self.sources["robots.txt"] += 1

        if rec.content_signal:
            self.content_signal_docs += 1
            for k, v in rec.content_signal.get("signals", {}).items():
                self.content_signals[f"{k}={v}"] += 1
        blocked = (rec.training_tokens or {}).get("blocked") or []
        if blocked:
            self.training_token_docs += 1
            self.training_tokens.update(blocked)
        if (rec.tdmrep or {}).get("reservation") == 1:
            self.tdmrep_reserved += 1
        if (rec.noai or {}).get("noai"):
            self.noai += 1
        self.licenses[(rec.license or {}).get("spdx") or ("other" if (rec.license or {}).get("url") else "none")] += 1

        if self.policy is None:
            self.policy = rec.policy
        if self.vocab is None:
            self.vocab = rec.vocab

    def extend(self, records: Iterable[ProvenanceRecord]) -> "AuditReport":
        for r in records:
            self.add(r)
        return self

    @classmethod
    def from_jsonl(cls, path: str) -> "AuditReport":
        rep = cls()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rep.add(ProvenanceRecord.from_dict(json.loads(line)))
        return rep

    def merge(self, other: "AuditReport") -> "AuditReport":
        out = AuditReport.from_dict(self.to_dict())
        for name in ("docs", "kept", "dropped", "tokens_kept", "tokens_dropped", "robots_time_mismatch",
                     "header_parse_errors", "robots_parse_errors", "tdmrep_reserved", "noai",
                     "content_signal_docs", "training_token_docs"):
            setattr(out, name, getattr(out, name) + getattr(other, name))
        for name in ("reasons", "reason_tokens", "dropped_domains", "sources", "robots_status", "licenses",
                     "content_signals", "training_tokens"):
            getattr(out, name).update(getattr(other, name))
        for cat, counts in other.combined.items():
            out.combined.setdefault(cat, Counter()).update(counts)
        out.policy = out.policy or other.policy
        out.vocab = out.vocab or other.vocab
        return out

    # Output ------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "tool": {"name": "usageledger", "version": __version__},
            "vocab": self.vocab,
            "policy": self.policy,
            "docs": self.docs,
            "kept": self.kept,
            "dropped": self.dropped,
            "tokens_kept": self.tokens_kept,
            "tokens_dropped": self.tokens_dropped,
            "reasons": dict(self.reasons),
            "reason_tokens": dict(self.reason_tokens),
            "dropped_domains": dict(self.dropped_domains),
            "combined": {k: dict(v) for k, v in self.combined.items()},
            "sources": dict(self.sources),
            "robots_status": dict(self.robots_status),
            "robots_time_mismatch": self.robots_time_mismatch,
            "header_parse_errors": self.header_parse_errors,
            "robots_parse_errors": self.robots_parse_errors,
            "tdmrep_reserved": self.tdmrep_reserved,
            "noai": self.noai,
            "content_signal_docs": self.content_signal_docs,
            "content_signals": dict(self.content_signals),
            "training_token_docs": self.training_token_docs,
            "training_tokens": dict(self.training_tokens),
            "licenses": dict(self.licenses),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuditReport":
        rep = cls()
        for name in ("docs", "kept", "dropped", "tokens_kept", "tokens_dropped", "robots_time_mismatch",
                     "header_parse_errors", "robots_parse_errors", "tdmrep_reserved", "noai",
                     "content_signal_docs", "training_token_docs"):
            setattr(rep, name, d.get(name, 0))
        for name in ("reasons", "reason_tokens", "dropped_domains", "sources", "robots_status", "licenses",
                     "content_signals", "training_tokens"):
            setattr(rep, name, Counter(d.get(name, {})))
        rep.combined = {k: Counter(v) for k, v in d.get("combined", {}).items()}
        rep.policy = d.get("policy")
        rep.vocab = d.get("vocab")
        return rep

    def to_json(self, path: str | None = None) -> str:
        text = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        return text

    def to_markdown(self, top: int = 10) -> str:
        def pct(n: int, d: int) -> str:
            return f"{100 * n / d:.1f}%" if d else "n/a"

        total_tokens = self.tokens_kept + self.tokens_dropped
        purpose = (self.policy or {}).get("purpose", "train-ai")
        lines = [
            "# usageledger audit report",
            "",
            f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by usageledger {__version__}, "
            f"vocabulary `{self.vocab}`.",
            "",
            "## Summary",
            "",
            "| | Documents | Est. tokens |",
            "|---|---:|---:|",
            f"| Kept | {self.kept:,} ({pct(self.kept, self.docs)}) | {self.tokens_kept:,} ({pct(self.tokens_kept, total_tokens)}) |",
            f"| Dropped | {self.dropped:,} ({pct(self.dropped, self.docs)}) | {self.tokens_dropped:,} ({pct(self.tokens_dropped, total_tokens)}) |",
            f"| **Total** | **{self.docs:,}** | **{total_tokens:,}** |",
            "",
            "Token counts are estimates (visible characters / 4) unless a tokenizer was supplied.",
            "",
            "## Why documents were dropped",
            "",
        ]
        if self.reasons:
            lines += ["| Reason | Documents | Est. tokens |", "|---|---:|---:|"]
            for reason, n in self.reasons.most_common():
                lines.append(f"| `{reason}` | {n:,} | {self.reason_tokens[reason]:,} |")
        else:
            lines.append("Nothing was dropped.")
        lines += ["", f"## Preferences for `{purpose}` and other categories", ""]
        if self.combined:
            lines += ["| Category | Allow | Disallow | Unknown |", "|---|---:|---:|---:|"]
            for cat, c in self.combined.items():
                lines.append(
                    f"| `{cat}` | {c['allow']:,} | {c['disallow']:,} | {c['unknown']:,} "
                    f"({pct(c['unknown'], self.docs)}) |"
                )
        lines += [
            "",
            "Documents with an explicit preference, by source: "
            + (", ".join(f"{k} {v:,}" for k, v in self.sources.most_common()) or "none")
            + ".",
            "",
            "## robots.txt coverage",
            "",
            "| Status | Documents |",
            "|---|---:|",
        ]
        for status, n in self.robots_status.most_common():
            lines.append(f"| {status} | {n:,} |")
        notes = []
        if self.robots_time_mismatch:
            notes.append(
                f"{_n(self.robots_time_mismatch, 'document')} judged against a robots.txt fetched *after* "
                "the page, because no earlier snapshot was available."
            )
        if self.header_parse_errors or self.robots_parse_errors:
            notes.append(
                f"Malformed preference values (treated as unknown): {self.header_parse_errors:,} in headers, "
                f"{self.robots_parse_errors:,} in robots.txt."
            )
        if notes:
            lines += [""] + [f"- {n}" for n in notes]
        lines += [
            "",
            "## Other signals",
            "",
            f"- Cloudflare `Content-Signal` in robots.txt: {_n(self.content_signal_docs, 'document')}"
            + (
                " (" + ", ".join(f"`{k}` {v:,}" for k, v in self.content_signals.most_common()) + ")"
                if self.content_signals
                else ""
            ),
            f"- Training-only tokens blocked in robots.txt: {_n(self.training_token_docs, 'document')}"
            + (
                " (" + ", ".join(f"`{k}` {v:,}" for k, v in self.training_tokens.most_common()) + ")"
                if self.training_tokens
                else ""
            ),
            f"- TDMRep `tdm-reservation: 1`: {_n(self.tdmrep_reserved, 'document')}",
            f"- `noai` directive: {_n(self.noai, 'document')}",
            "",
            "Detected licenses: "
            + ", ".join(f"{k} {v:,}" for k, v in self.licenses.most_common(top))
            + ".",
        ]
        if self.dropped_domains:
            lines += ["", f"## Domains with most dropped documents (top {top})", "", "| Domain | Dropped |", "|---|---:|"]
            for d, n in self.dropped_domains.most_common(top):
                lines.append(f"| {d} | {n:,} |")
        if self.policy:
            lines += ["", "## Policy used", "", "```json", json.dumps(self.policy, indent=2), "```"]
        return "\n".join(lines) + "\n"


def _n(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _reason_kind(reason: str | None) -> str:
    """Reasons are already low-cardinality (the detail in parentheses is the
    signal source), so they are aggregated as-is."""
    return reason or "unknown"
