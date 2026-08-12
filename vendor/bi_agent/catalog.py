"""Loads the metric catalog by parsing the semantic_layer/*.yml files directly.

Parsing the YAML (rather than dbt's manifest) means the catalog is available
without dbt having run, and it carries the full meta blocks the planner needs
for the system prompt and the explanation agent needs for citations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class MetricInfo:
    name: str
    label: str
    description: str
    type: str  # simple | ratio | derived
    meta: dict = field(default_factory=dict)


@dataclass
class DimensionInfo:
    qualified_name: str  # "<semantic_model>__<dimension>"
    semantic_model: str
    raw_name: str
    type: str  # categorical | time
    description: str = ""


@dataclass
class SemanticModelInfo:
    name: str
    description: str
    primary_entity: str = ""
    entities: list[str] = field(default_factory=list)
    dimension_names: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    metrics: dict[str, MetricInfo]
    dimensions: dict[str, DimensionInfo]
    semantic_models: dict[str, SemanticModelInfo]

    @property
    def time_dimension_raw_names(self) -> set[str]:
        return {d.raw_name for d in self.dimensions.values() if d.type == "time"}

    def is_known_metric(self, name: str) -> bool:
        return name in self.metrics

    def is_known_group_by(self, ref: str) -> bool:
        """Lenient check that tolerates MetricFlow's joined-dimension syntax.

        The leaf segment of e.g. `upsell_events__customer__partner__country`
        must be a known dimension name, a known entity, or `metric_time`.
        MetricFlow itself is the authoritative validator of the join path.
        """
        if ref == "metric_time" or ref.startswith("metric_time__"):
            return True
        if ref in self.dimensions:
            return True
        leaf = ref.split("__")[-1]
        known_leaves = {d.raw_name for d in self.dimensions.values()}
        for sm in self.semantic_models.values():
            known_leaves.update(sm.entities)
        return leaf in known_leaves


def load_catalog(semantic_layer_dir: Path) -> Catalog:
    if not semantic_layer_dir.is_dir():
        raise RuntimeError(f"Semantic layer directory not found: {semantic_layer_dir}")

    metrics: dict[str, MetricInfo] = {}
    dimensions: dict[str, DimensionInfo] = {}
    semantic_models: dict[str, SemanticModelInfo] = {}

    for yml_path in sorted(semantic_layer_dir.glob("*.yml")):
        doc = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}

        for sm in doc.get("semantic_models", []) or []:
            sm_name = sm["name"]
            entity_defs = sm.get("entities", []) or []
            entities = [e["name"] for e in entity_defs]
            # MetricFlow names a model's own dimensions <primary_entity>__<dim>,
            # NOT <semantic_model>__<dim>.
            primary_entity = next(
                (e["name"] for e in entity_defs if e.get("type") == "primary"),
                sm_name,
            )
            dim_names: list[str] = []
            for dim in sm.get("dimensions", []) or []:
                raw = dim["name"]
                qualified = f"{primary_entity}__{raw}"
                dimensions[qualified] = DimensionInfo(
                    qualified_name=qualified,
                    semantic_model=sm_name,
                    raw_name=raw,
                    type=dim.get("type", "categorical"),
                    description=_clean(dim.get("description", "")),
                )
                dim_names.append(qualified)
            semantic_models[sm_name] = SemanticModelInfo(
                name=sm_name,
                description=_clean(sm.get("description", "")),
                primary_entity=primary_entity,
                entities=entities,
                dimension_names=dim_names,
            )

        for m in doc.get("metrics", []) or []:
            name = m["name"]
            meta = ((m.get("config") or {}).get("meta")) or m.get("meta") or {}
            metrics[name] = MetricInfo(
                name=name,
                label=m.get("label", name),
                description=_clean(m.get("description", "")),
                type=m.get("type", "simple"),
                meta=meta,
            )

    if not metrics:
        raise RuntimeError(
            f"No metrics found in {semantic_layer_dir} — is the semantic layer built?"
        )
    return Catalog(metrics=metrics, dimensions=dimensions, semantic_models=semantic_models)


def _clean(text: str) -> str:
    return " ".join((text or "").split())
