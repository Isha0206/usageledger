"""datatrove pipeline blocks.

Typical pipeline over Common Crawl WARC files::

    from datatrove.executor import LocalPipelineExecutor
    from datatrove.pipeline.extractors import Trafilatura
    from datatrove.pipeline.writers import JsonlWriter
    from usageledger import Policy
    from usageledger.integrations.datatrove import (
        ProvenanceWarcReader, ContentUsageAnnotator, ContentUsageFilter, AuditReportWriter,
    )

    LocalPipelineExecutor(pipeline=[
        ProvenanceWarcReader("s3://commoncrawl/crawl-data/CC-MAIN-2026-.../warc/"),
        ContentUsageAnnotator(robots_warcs=["robotstxt.warc.gz"], policy=Policy(user_agent="CCBot")),
        AuditReportWriter("reports/"),
        ContentUsageFilter(),
        Trafilatura(),
        JsonlWriter("output/"),
    ], tasks=8).run()

The annotator has to run while ``doc.text`` is still HTML (before text
extraction), because it reads ``<meta>`` and license tags. The provenance
record is stored in ``doc.metadata["usageledger"]`` and travels with the
document through every later step.
"""

from __future__ import annotations

import json
from typing import Callable, Literal

from datatrove.data import Document, DocumentsPipeline
from datatrove.io import DataFolderLike, get_datafolder
from datatrove.pipeline.base import PipelineStep
from datatrove.pipeline.filters.base_filter import BaseFilter
from datatrove.pipeline.readers.warc import WarcReader, process_record

from ..audit import AuditReport
from ..provenance import Policy, ProvenanceRecord, evaluate
from ..robots import RobotsStore
from ..warc import parse_warc_date

__all__ = ["ProvenanceWarcReader", "ContentUsageAnnotator", "ContentUsageFilter", "AuditReportWriter", "METADATA_KEY"]

METADATA_KEY = "usageledger"
HEADERS_KEY = "http_headers"


class ProvenanceWarcReader(WarcReader):
    """datatrove's :class:`WarcReader`, but keeping the HTTP response headers.

    The stock reader drops them, and they are where ``Content-Usage``,
    ``tdm-reservation`` and ``Link: rel=license`` live. Headers are stored in
    ``doc.metadata["http_headers"]`` (lower-cased names, repeated fields
    joined with ", ") together with ``http_status``. Accepts the same
    arguments as :class:`WarcReader`.
    """

    name = "🕷 Warc + headers"

    def read_file(self, filepath: str):
        from warcio.archiveiterator import ArchiveIterator

        with self.data_folder.open(filepath, "rb", compression=self.compression) as f:
            for ri, record in enumerate(ArchiveIterator(f)):
                with self.track_time():
                    if record.rec_type == "response" and record.http_headers is not None:
                        headers: dict[str, str] = {}
                        for k, v in record.http_headers.headers:
                            k = k.lower()
                            headers[k] = f"{headers[k]}, {v}" if k in headers else v
                        status = record.http_headers.get_statuscode()
                    else:
                        headers, status = {}, None
                    extracted = process_record(record)
                    if not extracted:
                        continue
                    extracted[HEADERS_KEY] = headers
                    extracted["http_status"] = int(status) if status and status.isdigit() else None
                    document = self.get_document_from_dict(extracted, filepath, ri)
                    if not document:
                        continue
                yield document


class ContentUsageAnnotator(PipelineStep):
    """Attach a provenance record to every document, without dropping any.

    Args:
        policy: decision policy; see :class:`usageledger.Policy`.
        robots_store: a ready :class:`RobotsStore`, or
        robots_warcs: paths to robots.txt WARCs, loaded once per worker
            (preferred for large stores, so the store is not pickled).
        token_counter: optional callable text -> tokens for the estimate.
        metadata_format: ``"dict"`` stores the record as a nested dict
            (JSONL writers); ``"json"`` stores a JSON string (Parquet writers).
    """

    name = "🧾 Content-Usage annotator"
    type = "📝 - ANNOTATOR"

    def __init__(
        self,
        policy: Policy | None = None,
        robots_store: RobotsStore | None = None,
        robots_warcs: list[str] | None = None,
        token_counter: Callable[[str], int] | None = None,
        metadata_format: Literal["dict", "json"] = "dict",
    ):
        super().__init__()
        self.policy = policy or Policy()
        self.robots_store = robots_store
        self.robots_warcs = list(robots_warcs or [])
        self.token_counter = token_counter
        self.metadata_format = metadata_format

    def _store(self) -> RobotsStore | None:
        if self.robots_store is None and self.robots_warcs:
            self.robots_store = RobotsStore.from_warc(self.robots_warcs)
        return self.robots_store

    def annotate(self, doc: Document) -> ProvenanceRecord:
        md = doc.metadata
        rec = evaluate(
            md.get("url") or "",
            headers=md.get(HEADERS_KEY) or {},
            html=doc.text,
            fetched_at=parse_warc_date(md.get("date")),
            robots=self._store(),
            policy=self.policy,
            doc_id=doc.id,
            token_counter=self.token_counter,
        )
        md[METADATA_KEY] = rec.to_dict() if self.metadata_format == "dict" else json.dumps(rec.to_dict())
        return rec

    def run(self, data: DocumentsPipeline, rank: int = 0, world_size: int = 1) -> DocumentsPipeline:
        for doc in data:
            with self.track_time():
                rec = self.annotate(doc)
            self.stat_update("annotated")
            self.stat_update("would_keep" if rec.keep else "would_drop")
            for cat, pref in rec.combined.items():
                if pref != "unknown":
                    self.stat_update(f"{cat}={pref}")
            yield doc


def _record_of(doc: Document) -> dict | None:
    raw = doc.metadata.get(METADATA_KEY)
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


class ContentUsageFilter(BaseFilter):
    """Drop documents whose provenance record says ``keep: false``.

    Runs the annotator itself for documents that have no record yet, so it
    can also be used on its own. Dropped documents can be saved with
    ``exclusion_writer``; datatrove's stats then show one ``dropped_<reason>``
    counter per reason.
    """

    name = "🧾 Content-Usage filter"

    def __init__(self, annotator: ContentUsageAnnotator | None = None, exclusion_writer=None, **annotator_kwargs):
        super().__init__(exclusion_writer)
        self.annotator = annotator or ContentUsageAnnotator(**annotator_kwargs)

    def filter(self, doc: Document) -> bool | tuple[bool, str]:
        rec = _record_of(doc)
        if rec is None:
            rec = self.annotator.annotate(doc).to_dict()
        decision = rec.get("decision", {})
        if decision.get("keep", True):
            return True
        reasons = decision.get("reasons") or ["unknown"]
        return False, reasons[0].split(" (", 1)[0]


class AuditReportWriter(PipelineStep):
    """Pass documents through unchanged and write one audit report per rank.

    Reports land in ``output_folder`` as ``audit_<rank>.json`` plus a Markdown
    version; merge them with ``usageledger report audit_*.json``.
    """

    name = "🧾 Audit report"
    type = "📊 - STATS"

    def __init__(self, output_folder: DataFolderLike):
        super().__init__()
        self.output_folder = get_datafolder(output_folder)

    def run(self, data: DocumentsPipeline, rank: int = 0, world_size: int = 1) -> DocumentsPipeline:
        report = AuditReport()
        for doc in data:
            rec = _record_of(doc)
            if rec is not None:
                report.add(ProvenanceRecord.from_dict(rec))
            yield doc
        with self.output_folder.open(f"audit_{rank:05d}.json", "w") as fh:
            fh.write(report.to_json())
        with self.output_folder.open(f"audit_{rank:05d}.md", "w") as fh:
            fh.write(report.to_markdown())
