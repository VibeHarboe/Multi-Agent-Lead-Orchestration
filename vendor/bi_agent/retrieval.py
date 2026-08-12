"""RAG over metric definitions — forward-looking infrastructure.

At the current scale (~51 metrics) the whole catalog fits comfortably in the
planner's prompt, so retrieval is OFF by default (RETRIEVAL_K=0) and the full
catalog is prompt-cached instead. This module exists for when the metric set
outgrows a single prompt (10+ markets): set RETRIEVAL_K > 0 and the planner
builds its prompt from only the top-K relevant metrics per question.
See ARCHITECTURE.md section 13.

Uses Chroma with its default local embedding model (all-MiniLM-L6-v2) — offline,
no API key. `chromadb` is imported lazily, so the agent runs without it whenever
retrieval is disabled.
"""

from __future__ import annotations

from pathlib import Path

from .catalog import Catalog, MetricInfo

_COLLECTION = "metric_definitions"


def _document(metric: MetricInfo) -> str:
    """The text a metric is embedded as — name, label, level, description."""
    level = metric.meta.get("level", "")
    return f"{metric.name} | {metric.label} | level: {level} | {metric.description}"


class MetricRetriever:
    """Embeds the metric catalog into a local Chroma index and retrieves the
    metrics most relevant to a question."""

    def __init__(self, catalog: Catalog, persist_dir: Path):
        self.catalog = catalog
        self.persist_dir = persist_dir
        self._collection = None

    def _collection_handle(self):
        if self._collection is not None:
            return self._collection

        import chromadb  # lazy — only required when retrieval is enabled

        client = chromadb.PersistentClient(path=str(self.persist_dir))
        collection = client.get_or_create_collection(_COLLECTION)

        # Rebuild when the index does not match the current catalog size.
        # Note: this does not detect an edited definition at the same metric
        # count — delete the .chroma/ directory to force a full rebuild then.
        if collection.count() != len(self.catalog.metrics):
            client.delete_collection(_COLLECTION)
            collection = client.create_collection(_COLLECTION)
            names = list(self.catalog.metrics)
            collection.add(
                ids=names,
                documents=[_document(self.catalog.metrics[n]) for n in names],
            )

        self._collection = collection
        return collection

    def retrieve(self, question: str, k: int) -> list[str]:
        """Return the names of the k metrics most relevant to the question."""
        collection = self._collection_handle()
        k = max(1, min(k, collection.count()))
        result = collection.query(query_texts=[question], n_results=k)
        return list(result["ids"][0])
