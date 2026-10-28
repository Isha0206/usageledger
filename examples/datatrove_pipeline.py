"""Run usageledger inside a datatrove pipeline on the built-in demo corpus.

    pip install "usageledger[datatrove]"
    python examples/datatrove_pipeline.py

Swap the demo paths for real Common Crawl WARCs (pages) and the matching
robotstxt WARCs to run it on actual data.
"""

from datatrove.executor import LocalPipelineExecutor
from datatrove.pipeline.writers import JsonlWriter

from usageledger import Policy
from usageledger.demo import write_demo_corpus
from usageledger.integrations.datatrove import (
    AuditReportWriter,
    ContentUsageAnnotator,
    ContentUsageFilter,
    ProvenanceWarcReader,
)

if __name__ == "__main__":
    pages, robots = write_demo_corpus("pipeline-demo/input")

    LocalPipelineExecutor(
        pipeline=[
            ProvenanceWarcReader("pipeline-demo/input", glob_pattern="pages.warc.gz"),
            ContentUsageAnnotator(robots_warcs=[robots], policy=Policy(user_agent="CCBot")),
            AuditReportWriter("pipeline-demo/reports"),
            ContentUsageFilter(exclusion_writer=JsonlWriter("pipeline-demo/dropped", compression=None)),
            # A real pipeline would extract text here, e.g. datatrove's Trafilatura().
            JsonlWriter("pipeline-demo/kept", compression=None),
        ],
        tasks=1,
        logging_dir="pipeline-demo/logs",
    ).run()

    print(open("pipeline-demo/reports/audit_00000.md").read())
