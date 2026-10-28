# Changelog

## 0.3.0 — 2026-10-28

- `usageledger survey sites.txt`: polite sequential checks of a site list, a CSV with one row per site, and a Markdown summary by category (crawler-blocking posture, share blocking each crawler, adoption of each usage signal, per-site table).
- Training-only tokens: explicit robots.txt blocks on Google-Extended and Applebot-Extended are recorded (`training_tokens`), shown by `check`, counted in audit reports, and honoured for `train-ai` by default (`Policy.honor_training_tokens`, `--ignore-training-tokens`).
- `check`: a site without robots.txt now reports every AI crawler as allowed, instead of nothing.
- `examples/sites.txt`: starter list of about 55 Swiss and international sites.

## 0.2.0 — 2026-10-28

- `usageledger check <url>`: live check of robots.txt and the page, with an AI-crawler access summary. Polite by design: checks robots.txt for its own token first and for every redirect hop, one request per URL.
- Cloudflare `Content-Signal` robots.txt rule: parsed, recorded in a separate `content_signal` field, honoured by default (`Policy.honor_content_signal`, `--ignore-content-signal`), and counted in audit reports.
- `RobotsStore.snapshot()`.
- Demo corpus: one Cloudflare-style site (18 pages).

## 0.1.0 — 2026-10-28

First release.

- Strict RFC 9651 Structured Field Dictionary parser.
- AIPREF vocabulary (draft-ietf-aipref-vocab-08): `train-ai`, `ai-use`, `search`; statement parsing and most-restrictive combination.
- `Content-Usage` HTTP header and robots.txt rule (draft-ietf-aipref-attach-05), with RFC 9309 group selection and longest-match paths.
- Time-aware `RobotsStore`, loadable from Common Crawl robots.txt WARCs.
- TDMRep, `noai` and Creative Commons license signals.
- Per-document provenance records, explicit `Policy`, mergeable `AuditReport`.
- CLI: `scan`, `report`, `header`, `robots`, `demo`.
- datatrove blocks: `ProvenanceWarcReader`, `ContentUsageAnnotator`, `ContentUsageFilter`, `AuditReportWriter`.
