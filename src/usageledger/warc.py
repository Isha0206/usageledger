"""Reading pages, with their HTTP headers, out of WARC files.

The HTTP response headers are what carry ``Content-Usage``, ``tdm-reservation``
and ``Link: rel=license``. Common Crawl's WARC files keep them, which is what
makes after-the-fact preference checks possible; WET/WAT-style extracts and
most cleaned datasets throw them away.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

__all__ = ["Page", "iter_responses", "parse_warc_date"]


@dataclass
class Page:
    url: str
    fetched_at: datetime | None
    body: bytes
    http_status: int | None = None
    # Every header line, in order, with lower-cased names. Kept as a list so
    # repeated fields (several Content-Usage lines) are not lost.
    header_lines: list[tuple[str, str]] = field(default_factory=list)
    record_id: str | None = None
    warc_path: str | None = None

    def header(self, name: str) -> str | None:
        """All lines of a field joined with ", " (RFC 9110 Section 5.3)."""
        name = name.lower()
        values = [v for k, v in self.header_lines if k == name]
        return ", ".join(values) if values else None

    @property
    def headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, _ in self.header_lines:
            if k not in out:
                out[k] = self.header(k)
        return out

    @property
    def content_type(self) -> str:
        return (self.header("content-type") or "").split(";")[0].strip().lower()

    @property
    def charset(self) -> str:
        for part in (self.header("content-type") or "").split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                return v.strip("\"' ")
        return "utf-8"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        try:
            return self.body.decode(self.charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def parse_warc_date(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iter_responses(path: str) -> Iterator[Page]:
    """Yield every HTTP ``response`` record in a (possibly gzipped) WARC."""
    try:
        from warcio.archiveiterator import ArchiveIterator
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Reading WARC files needs warcio: pip install 'usageledger[warc]'") from exc

    with open(path, "rb") as fh:
        for record in ArchiveIterator(fh):
            if record.rec_type != "response":
                continue
            url = record.rec_headers.get_header("WARC-Target-URI")
            if not url:
                continue
            status = None
            lines: list[tuple[str, str]] = []
            if record.http_headers is not None:
                code = record.http_headers.get_statuscode()
                status = int(code) if code and code.isdigit() else None
                lines = [(k.lower(), v) for k, v in record.http_headers.headers]
            yield Page(
                url=url,
                fetched_at=parse_warc_date(record.rec_headers.get_header("WARC-Date")),
                body=record.content_stream().read(),
                http_status=status,
                header_lines=lines,
                record_id=record.rec_headers.get_header("WARC-Record-ID"),
                warc_path=path,
            )
