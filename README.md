# usageledger

**Carry AI usage preferences and provenance through web-data pipelines.**

Websites can now say *"you may crawl this, but don't train AI on it"* with the IETF's new `Content-Usage` signal, in robots.txt or an HTTP header. The preference is only visible at crawl time. By the time a page is cleaned, deduplicated, mixed into a corpus and trained on months later, nobody remembers which site it came from or what the owner allowed.

`usageledger` reads those signals while the page is still raw, attaches a small provenance record to every document, decides keep or drop under an explicit policy, and writes an audit report of what was removed and why.

```
$ usageledger demo
| | Documents | Est. tokens |
|---|---:|---:|
| Kept | 11 (61.1%) | 3,212 (61.1%) |
| Dropped | 7 (38.9%) | 2,049 (38.9%) |

| Reason | Documents | Est. tokens |
|---|---:|---:|
| `aipref:train-ai=n (robots.txt)` | 3 | 881 |
| `aipref:train-ai=n (http-header)` | 1 | 290 |
| `tdmrep:reserved (header)` | 1 | 289 |
| `robots:disallow (CCBot)` | 1 | 292 |
| `content-signal:ai-train=no (robots.txt)` | 1 | 297 |
```

Or point it at real sites:

```
$ usageledger check https://some-news-site.example/story
  robots.txt: found (200)   page: 200
  Content-Usage   header: none   robots.txt: none
  Content-Signal  robots.txt: search=yes, ai-train=no
  TDMRep: none   noai: none   license: none
  AI crawlers blocked here: GPTBot, CCBot
  Decision for train-ai as *: DROP - content-signal:ai-train=no (robots.txt)
```

## What it does

- **Parses AIPREF preferences** exactly as specified in [draft-ietf-aipref-vocab-08](https://datatracker.ietf.org/doc/draft-ietf-aipref-vocab/) and [draft-ietf-aipref-attach-05](https://datatracker.ietf.org/doc/draft-ietf-aipref-attach/): the `Content-Usage` HTTP header and the robots.txt `Content-Usage` rule, including path-specific rules, user-agent groups and the "most restrictive wins" combination rule. It ships a strict [RFC 9651](https://www.rfc-editor.org/rfc/rfc9651) parser, because the spec says a malformed value means *every* preference is unknown.
- **Uses the robots.txt that was current when the page was fetched.** Preferences don't apply retroactively, so a page fetched in April is judged against April's robots.txt, not June's. Mismatches are flagged.
- **Records other signals** alongside AIPREF: Cloudflare's `Content-Signal` robots.txt rule (common on live sites), blocks on the training-only tokens Google-Extended and Applebot-Extended, TDMRep `tdm-reservation` (the opt-out most relevant under EU law), the non-standard `noai` directive, and Creative Commons licenses mapped to SPDX ids.
- **Attaches a JSON provenance record** to every document: URL, fetch time, content hash, every signal seen, the decision and the exact policy that produced it.
- **Produces an audit report** (Markdown + mergeable JSON): what was dropped, why, how many tokens it cost, which domains, and how much of the corpus stated any preference at all.
- **Checks live sites** with `usageledger check <url>`: fetches robots.txt and the page politely, and also shows which well-known AI crawlers (GPTBot, ClaudeBot, CCBot, …) the site blocks.
- **Surveys many sites** with `usageledger survey sites.txt`: one CSV row per site plus a Markdown summary of how sites, by category, signal AI preferences.
- **Plugs into [datatrove](https://github.com/huggingface/datatrove)**, Hugging Face's pipeline library (used to build FineWeb), or runs standalone on WARC files from the command line.

No runtime dependencies for the core library. `warcio` is needed to read WARC files, and `datatrove` for the pipeline blocks.

## Install

```bash
pip install "usageledger[warc]"        # core + WARC reading + CLI
pip install "usageledger[datatrove]"   # + datatrove pipeline blocks
```

Until it's on PyPI, install from the source folder: `pip install -e ".[warc]"`.

## Try it in one command

```bash
usageledger demo ./demo
```

This writes a small synthetic crawl (18 pages on `.example` domains, plus their robots.txt captures) that exercises every rule: path-specific opt-outs, a header opt-out, a bot-specific group, a site that changed its robots.txt mid-crawl, a typo'd header, a Cloudflare `Content-Signal`, TDMRep, `noai`, CC licenses, missing and broken robots.txt. It scans it and writes `provenance.jsonl`, `report.md` and `report.json` into `./demo`.

## Use it as a library

```python
from usageledger import parse_statement, RobotsTxt, RobotsStore, Policy, evaluate

parse_statement("train-ai=n, search=y").prefs
# {'train-ai': <Preference.DISALLOW>, 'ai-use': <Preference.UNKNOWN>, 'search': <Preference.ALLOW>}

robots = RobotsTxt.parse("""
User-Agent: *
Disallow: /never/
Content-Usage: train-ai=n
Content-Usage: /ai-ok/ train-ai=y
""")
robots.evaluate("/ai-ok/page", user_agent="CCBot").statement.get("train-ai")   # ALLOW
robots.evaluate("/news/page", user_agent="CCBot").statement.get("train-ai")    # DISALLOW
robots.evaluate("/never/page", user_agent="CCBot").crawlable                    # False

record = evaluate(
    "https://news.example/story",
    headers={"content-usage": "search=y"},
    html=page_html,
    fetched_at=fetch_time,
    robots=RobotsStore.from_warc(["robotstxt.warc.gz"]),
    policy=Policy(purpose="train-ai", user_agent="CCBot"),
)
record.keep, record.reasons
# (False, ['aipref:train-ai=n (robots.txt)'])
```

## Check live sites

```bash
usageledger check https://www.example.com/some/article https://en.wikipedia.org/wiki/Zurich
usageledger check example.com --user-agent GPTBot          # judge as a specific crawler
usageledger check example.com --no-page --out live.jsonl   # robots.txt only, save records
```

For each URL it fetches the site's robots.txt, then the page, and prints what the site states: `Content-Usage` (header and robots.txt), Cloudflare `Content-Signal`, TDMRep, `noai`, the license, which well-known AI crawlers may fetch that URL, and the keep/drop decision. `--out` writes full provenance records.

It is built for spot checks, not crawling. It sends one request per URL with a `usageledger/<version>` User-Agent, and it only fetches a page if robots.txt allows `usageledger`. Every redirect hop is checked against its own site's robots.txt before it is followed, and if robots.txt is unreachable the page is not fetched (RFC 9309).

Expect most sites to show **no** IETF `Content-Usage` yet: it is still a draft and barely adopted. What you will see much more often is AI crawlers blocked by name in robots.txt, and on Cloudflare-hosted sites a `Content-Signal` line.

## Survey a list of sites

```bash
usageledger survey examples/sites.txt --csv survey.csv --report survey.md
```

`examples/sites.txt` is a starter list of about 55 sites in four categories: Swiss news, Swiss universities, international news, and reference sites. The format is one site per line with an optional category. Each site gets the same polite check as `usageledger check`, one at a time with a one-second pause (`--delay`). The run takes a few minutes.

The summary has four tables: how many AI crawlers each site blocks (none, some, most or all), the share of sites blocking each crawler, how many sites use each machine-readable usage signal, and a per-site table. Every table is split by category. The CSV has one row per site with every field, ready for pandas or a spreadsheet.

## Scan Common Crawl from the command line

Common Crawl publishes its robots.txt captures for every crawl as WARC files, listed in `robotstxt.paths.gz` next to `warc.paths.gz`. Download one page WARC and the matching robots.txt WARCs, then:

```bash
usageledger scan CC-MAIN-...warc.gz \
    --robots robotstxt/*.warc.gz \
    --user-agent CCBot \
    --out provenance.jsonl --report report.md --json-report report.json
```

Use `--user-agent CCBot` for Common Crawl data: the robots.txt group that applies is the one for the crawler that actually fetched the page. Reports from parallel runs can be merged with `usageledger report part-*.json --report total.md`.

Other commands: `usageledger header "train-ai=n"` parses one header value, and `usageledger robots robots.txt /some/path --user-agent CCBot` checks one path.

## Use it in a datatrove pipeline

```python
from datatrove.executor import LocalPipelineExecutor
from datatrove.pipeline.extractors import Trafilatura
from datatrove.pipeline.writers import JsonlWriter
from usageledger import Policy
from usageledger.integrations.datatrove import (
    ProvenanceWarcReader, ContentUsageAnnotator, ContentUsageFilter, AuditReportWriter,
)

LocalPipelineExecutor(pipeline=[
    ProvenanceWarcReader("warcs/"),                     # like WarcReader, but keeps HTTP headers
    ContentUsageAnnotator(robots_warcs=["robotstxt.warc.gz"], policy=Policy(user_agent="CCBot")),
    AuditReportWriter("reports/"),                      # one report per worker
    ContentUsageFilter(exclusion_writer=JsonlWriter("dropped/")),
    Trafilatura(),                                      # text extraction comes after
    JsonlWriter("output/"),
], tasks=8).run()
```

The annotator must run **before** text extraction, while the document is still HTML, because it reads `<meta>` and license tags. The record is stored in `doc.metadata["usageledger"]` and travels with each document through the rest of the pipeline. The stock `WarcReader` discards HTTP headers, which is why `ProvenanceWarcReader` exists.

## The provenance record

One per document (abridged):

```json
{
  "schema": "usageledger/provenance/v1",
  "url": "https://changed.example/may",
  "fetched_at": "2026-05-10T09:00:00+00:00",
  "content_sha256": "9f2c…",
  "est_tokens": 294,
  "aipref": {
    "vocab": "draft-ietf-aipref-vocab-08",
    "header": null,
    "robots": {
      "origin": "https://changed.example", "status": "ok",
      "snapshot_fetched_at": "2026-05-01T00:00:00+00:00", "time_matched": true, "stale": true,
      "crawlable": true, "group": "*",
      "matched_rules": [{"path": null, "value": "train-ai=n"}],
      "statement": {"source": "robots.txt", "raw": "train-ai=n", "parse_ok": true,
                    "prefs": {"train-ai": "disallow", "ai-use": "unknown", "search": "unknown"}}
    },
    "combined": {"train-ai": "disallow", "ai-use": "unknown", "search": "unknown"}
  },
  "tdmrep": {"reservation": null, "policy": null, "source": null},
  "noai": {"noai": false, "noimageai": false, "sources": {}},
  "license": {"url": null, "spdx": null, "source": null},
  "decision": {"keep": false, "reasons": ["aipref:train-ai=n (robots.txt)"], "policy": {"purpose": "train-ai", "…": "…"}},
  "tool": {"name": "usageledger", "version": "0.3.0"}
}
```

## Policy

The drafts deliberately leave some choices to the data user, such as what to do when a preference is unknown. Every such choice is an explicit `Policy` field, and the policy is copied into every record.

| Field | Default | Meaning |
|---|---|---|
| `purpose` | `"train-ai"` | Which usage category decides keep/drop (`"ai-use"` for retrieval, `"search"` for search). |
| `unknown` | `"keep"` | Keep or drop documents with no stated preference for `purpose`. |
| `user_agent` | `"*"` | Which robots.txt group applies. Use the crawler that fetched the data, e.g. `CCBot`. |
| `drop_if_robots_disallowed` | `True` | Drop pages robots.txt did not allow this user agent to crawl. |
| `drop_if_robots_unreachable` | `False` | Drop pages whose robots.txt returned a 5xx (RFC 9309 treats that as "disallow all"). |
| `honor_content_signal` | `True` | Treat Cloudflare's `Content-Signal: ai-train=no` (or the key matching `purpose`) as an opt-out. |
| `honor_training_tokens` | `True` | For `train-ai`, treat a robots.txt group that explicitly names and blocks Google-Extended or Applebot-Extended as an opt-out. |
| `honor_tdmrep` | `True` | Treat `tdm-reservation: 1` as an opt-out. |
| `honor_noai` | `False` | Treat the non-standard `noai` directive as an opt-out. |
| `exclude_licenses` | `()` | Drop documents whose license id contains any of these, e.g. `("-NC-", "-ND-")`. |

## How it reads the specs

- **Vocabulary:** `train-ai`, `ai-use`, `search` from vocab-08. Values are only the tokens `y`/`n`. Unknown labels and parameters are ignored, and any parse error makes every category unknown. Vocabularies are versioned objects (`VOCAB_DRAFT_08`), so a future draft can be added without breaking old records.
- **Combining:** across the header and robots.txt, and across identical robots.txt paths, any `n` wins, then any `y`, otherwise unknown.
- **robots.txt:** RFC 9309 group selection (exact product token, else `*`; same-token groups merged), longest-match Allow/Disallow with `*` and `$`, and Content-Usage rules matched the same way. A rule without a path matches everything with the lowest priority. If two different patterns of equal length both match, their preferences are combined (the draft only addresses identical paths).
- **Repeated header lines** are joined with `", "` before parsing, as RFC 9651 requires.
- **Leniency:** SP and HTAB are trimmed around the statement in robots.txt rules. Otherwise parsing is strict.
- **Cloudflare `Content-Signal`:** parsed from robots.txt with the same group and path matching, and mapped `ai-train`→`train-ai`, `ai-input`→`ai-use`, `search`→`search`. That mapping is ours, not part of either spec, so the signal is kept in its own `content_signal` field and never mixed into the AIPREF result.
- **Training-only tokens:** Google-Extended and Applebot-Extended never crawl; Google and Apple created them only so sites can opt out of AI training without leaving search. A block counts only when robots.txt names the token explicitly. A general `User-agent: * / Disallow: /` does not count as an AI opt-out. Treating such a block as a general training opt-out, not just one for that vendor, is our reading, which is why it has its own setting.
- **robots.txt statuses:** 2xx is parsed, 4xx means no rules, 5xx means "unreachable", and redirects are followed when the target is also in the store.

## Limitations

- The AIPREF documents are **Internet-Drafts** and still changing; the vocabulary has had nine revisions. Records carry the vocabulary version so they stay interpretable, but expect updates.
- Only preferences attached via HTTP are read. Embedded metadata (C2PA, XMP), preference registries and `/.well-known/tdmrep.json` are not yet supported.
- Token counts are a rough estimate (visible characters / 4) unless you pass a `token_counter`.
- This tool records and applies stated preferences. It is not legal advice and does not decide what the law requires.

## Roadmap

- `/.well-known/tdmrep.json` and C2PA support
- Parquet-friendly flattened records
- A benchmark notebook: measure opt-out rates on a real Common Crawl sample and reproduce the token-cost analysis in [Fan et al., *Can Performant LLMs Be Ethical?*](https://arxiv.org/abs/2504.06219)
- Publish to PyPI

## Development

```bash
pip install -e ".[dev,datatrove]"
pytest
```

The tests cover the spec examples from both drafts, RFC 9651 edge cases, robots.txt matching, time-aware snapshots, the CLI, and a full datatrove pipeline over the demo corpus.

## License

Apache 2.0
