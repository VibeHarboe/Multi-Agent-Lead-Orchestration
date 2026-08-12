"""Observability — the trace-per-lead layer (§10).

A Langfuse-compatible local trace recorder: every graph run is a *trace*,
every node a *span*, every MCP tool call a *child span* — the exact tree
shape ARCHITECTURE §10 sketches. Traces are held in memory and can be saved
as JSON under `.langfuse/` for inspection.

Live forwarding: when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
LANGFUSE_HOST are set *and* the `langfuse` SDK is installed, finished traces
are forwarded 1:1 (lazy import — the SDK is never required for local runs or
tests). The recorder's local tree is the source of truth either way, which is
what the Week 5 exit gate asserts against.

Usage:
    rec = TraceRecorder()
    with rec.trace("lead_run:RL00042", lead_id="RL00042"):
        with rec.span("intake_gate"):
            ...
        with rec.span("match"):
            with rec.span("mcp.partner_capacity.list_candidates"):
                ...
    rec.shape()   # -> nested name tree for reference assertions
    rec.save()    # -> .langfuse/<trace-name>.json
"""

from __future__ import annotations

import contextvars
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

_current_span: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "observability_current_span", default=None)


@dataclass
class Span:
    name: str
    meta: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)

    def to_dict(self) -> dict:
        return {"name": self.name, "meta": self.meta,
                "duration_ms": self.duration_ms,
                "children": [c.to_dict() for c in self.children]}

    def shape(self):
        """Just the nested names — the reference-tree format tests assert."""
        if not self.children:
            return self.name
        return {self.name: [c.shape() for c in self.children]}


class TraceRecorder:
    def __init__(self, out_dir: Path | str = ".langfuse"):
        self.out_dir = Path(out_dir)
        self.traces: list[Span] = []

    # ── recording ───────────────────────────────────────────────────────
    @contextmanager
    def trace(self, name: str, **meta):
        root = Span(name=name, meta=meta)
        self.traces.append(root)
        token = _current_span.set(root)
        try:
            yield root
        finally:
            root.ended_at = time.time()
            _current_span.reset(token)
            self._forward(root)

    @contextmanager
    def span(self, name: str, **meta):
        parent = _current_span.get()
        node = Span(name=name, meta=meta)
        if parent is not None:
            parent.children.append(node)
        else:
            self.traces.append(node)     # orphan span becomes its own trace
        token = _current_span.set(node)
        try:
            yield node
        finally:
            node.ended_at = time.time()
            _current_span.reset(token)

    # ── inspection ──────────────────────────────────────────────────────
    def shape(self) -> list:
        return [t.shape() for t in self.traces]

    def last_trace(self) -> Span | None:
        return self.traces[-1] if self.traces else None

    def save(self) -> list[Path]:
        self.out_dir.mkdir(exist_ok=True)
        paths = []
        for i, t in enumerate(self.traces):
            safe = t.name.replace(":", "_").replace("/", "_")
            p = self.out_dir / f"{safe}_{i}.json"
            p.write_text(json.dumps(t.to_dict(), indent=2, default=str),
                         encoding="utf-8")
            paths.append(p)
        return paths

    # ── optional live forwarding ────────────────────────────────────────
    def _forward(self, root: Span) -> None:
        if not (os.environ.get("LANGFUSE_PUBLIC_KEY")
                and os.environ.get("LANGFUSE_SECRET_KEY")):
            return
        try:
            from langfuse import Langfuse          # lazy — optional dependency
        except ImportError:
            return
        client = Langfuse()

        def _emit(span: Span, parent=None):
            obj = (client.trace(name=span.name, metadata=span.meta)
                   if parent is None else
                   parent.span(name=span.name, metadata=span.meta))
            for child in span.children:
                _emit(child, obj)

        _emit(root)
        client.flush()
