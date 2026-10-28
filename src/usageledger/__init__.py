"""usageledger: carry AI usage preferences and provenance through data pipelines.

Quick start::

    from usageledger import parse_statement, RobotsTxt, Policy, evaluate

    parse_statement("train-ai=n, search=y")
    RobotsTxt.parse(open("robots.txt").read()).evaluate("/blog/post", "CCBot")
    record = evaluate(url, headers=headers, html=html, policy=Policy(purpose="train-ai"))
"""

__version__ = "0.3.0"

from .vocab import (  # noqa: E402
    DEFAULT_VOCAB,
    VOCAB_DRAFT_08,
    Category,
    Preference,
    Statement,
    Vocabulary,
    combine,
    parse_statement,
)
from .robots import RobotsStore, RobotsTxt, origin_of  # noqa: E402
from .provenance import Policy, ProvenanceRecord, evaluate, evaluate_page  # noqa: E402
from .audit import AuditReport  # noqa: E402

__all__ = [
    "__version__",
    "DEFAULT_VOCAB",
    "VOCAB_DRAFT_08",
    "Category",
    "Preference",
    "Statement",
    "Vocabulary",
    "combine",
    "parse_statement",
    "RobotsStore",
    "RobotsTxt",
    "origin_of",
    "Policy",
    "ProvenanceRecord",
    "evaluate",
    "evaluate_page",
    "AuditReport",
]
