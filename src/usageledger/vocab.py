"""The AI usage preference vocabulary (draft-ietf-aipref-vocab).

The vocabulary is still an Internet-Draft, so it is modelled as a versioned,
swappable object rather than hard-coded constants. Every provenance record
names the vocabulary version it was evaluated against, so results stay
interpretable after the draft changes.

Data model (vocab-08, Sections 3 and 5):

* A *statement of preference* assigns ``allow`` or ``disallow`` to some usage
  categories. Categories it does not mention are ``unknown``.
* Several statements about one asset are combined per category: any
  ``disallow`` wins, otherwise any ``allow``, otherwise ``unknown``.
* In the exemplary serialization (Section 6) a statement is an RFC 9651
  Dictionary whose keys are category labels and whose values are the tokens
  ``y`` or ``n``. Anything else - other value types, unknown labels,
  parameters - is ignored, and a parse failure makes everything unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping

from . import sfv

__all__ = [
    "Preference",
    "Category",
    "Vocabulary",
    "Statement",
    "VOCAB_DRAFT_08",
    "DEFAULT_VOCAB",
    "parse_statement",
    "combine",
]


class Preference(str, Enum):
    ALLOW = "allow"
    DISALLOW = "disallow"
    UNKNOWN = "unknown"

    @property
    def symbol(self) -> str:
        return {"allow": "y", "disallow": "n", "unknown": "?"}[self.value]


@dataclass(frozen=True)
class Category:
    label: str
    name: str
    description: str


@dataclass(frozen=True)
class Vocabulary:
    """A named, versioned set of usage categories."""

    version: str
    categories: tuple[Category, ...]
    reference: str = ""

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(c.label for c in self.categories)

    def __contains__(self, label: object) -> bool:
        return label in self.labels


VOCAB_DRAFT_08 = Vocabulary(
    version="draft-ietf-aipref-vocab-08",
    reference="https://datatracker.ietf.org/doc/html/draft-ietf-aipref-vocab-08",
    categories=(
        Category(
            "train-ai",
            "AI Training",
            "Using an asset to modify the learned parameters of a generative AI model.",
        ),
        Category(
            "ai-use",
            "AI Use",
            "Using an asset as input to a generative AI model, where the asset is "
            "not directly provided by the user.",
        ),
        Category(
            "search",
            "Search",
            "Using an asset in an application whose primary purpose is to select "
            "assets and direct users to their location.",
        ),
    ),
)

DEFAULT_VOCAB = VOCAB_DRAFT_08


@dataclass
class Statement:
    """One statement of preference, plus where it came from.

    ``prefs`` holds an entry for every category in ``vocab``. ``parse_ok`` is
    False when the raw value was not a valid Dictionary; per the draft that
    makes every category unknown, but the raw value is kept for auditing.
    """

    prefs: dict[str, Preference]
    vocab: Vocabulary = DEFAULT_VOCAB
    raw: str | None = None
    parse_ok: bool = True
    source: str = ""
    ignored_labels: list[str] = field(default_factory=list)

    def get(self, label: str) -> Preference:
        return self.prefs.get(label, Preference.UNKNOWN)

    @property
    def is_empty(self) -> bool:
        return all(p is Preference.UNKNOWN for p in self.prefs.values())

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "raw": self.raw,
            "parse_ok": self.parse_ok,
            "prefs": {k: v.value for k, v in self.prefs.items()},
            **({"ignored_labels": self.ignored_labels} if self.ignored_labels else {}),
        }

    def __str__(self) -> str:
        known = [f"{k}={v.symbol}" for k, v in self.prefs.items() if v is not Preference.UNKNOWN]
        return ", ".join(known) if known else "(no known preferences)"


def unknown_statement(vocab: Vocabulary = DEFAULT_VOCAB, **kwargs) -> Statement:
    return Statement({c: Preference.UNKNOWN for c in vocab.labels}, vocab=vocab, **kwargs)


def parse_statement(
    value: bytes | str | None,
    vocab: Vocabulary = DEFAULT_VOCAB,
    source: str = "",
) -> Statement:
    """Parse a serialized statement of preference (vocab-08 Section 6.5).

    Never raises: invalid input yields a statement where everything is unknown
    and ``parse_ok`` is False.
    """
    if value is None:
        return unknown_statement(vocab, source=source)
    raw = value.decode("latin-1") if isinstance(value, bytes) else value
    try:
        members = sfv.parse_dictionary(value)
    except sfv.ParseError:
        return unknown_statement(vocab, raw=raw, parse_ok=False, source=source)

    prefs: dict[str, Preference] = {}
    for label in vocab.labels:
        member = members.get(label)
        pref = Preference.UNKNOWN
        if isinstance(member, sfv.Item) and isinstance(member.value, sfv.Token):
            if member.value == "y":
                pref = Preference.ALLOW
            elif member.value == "n":
                pref = Preference.DISALLOW
        prefs[label] = pref
    ignored = [k for k in members if k not in vocab]
    return Statement(prefs, vocab=vocab, raw=raw, parse_ok=True, source=source, ignored_labels=ignored)


def combine(
    statements: Iterable[Statement | Mapping[str, Preference]],
    vocab: Vocabulary = DEFAULT_VOCAB,
) -> dict[str, Preference]:
    """Combine statements per category, most restrictive first (Section 5.1)."""
    stmts = list(statements)
    out: dict[str, Preference] = {}
    for label in vocab.labels:
        values = [
            (s.get(label) if isinstance(s, Statement) else s.get(label, Preference.UNKNOWN))
            for s in stmts
        ]
        if Preference.DISALLOW in values:
            out[label] = Preference.DISALLOW
        elif Preference.ALLOW in values:
            out[label] = Preference.ALLOW
        else:
            out[label] = Preference.UNKNOWN
    return out
