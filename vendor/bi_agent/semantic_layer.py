"""Thin wrapper around the MetricFlow `mf` CLI.

This is the single seam where you would later swap to the dbt Semantic Layer
SDK / GraphQL API (dbt Cloud) without touching the planner.
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class MetricFlowError(RuntimeError):
    """Raised when an `mf` invocation fails — message carries mf's stderr."""


@dataclass
class QuerySpec:
    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    where: str | None = None
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None

    def to_mf_args(self) -> list[str]:
        args = ["query", "--metrics", ",".join(self.metrics)]
        if self.group_by:
            args += ["--group-by", ",".join(self.group_by)]
        if self.where:
            args += ["--where", self.where]
        if self.order_by:
            args += ["--order", ",".join(self.order_by)]
        if self.limit:
            args += ["--limit", str(self.limit)]
        return args


class MetricFlowClient:
    def __init__(self, dbt_project_dir: Path):
        self.dbt_project_dir = dbt_project_dir

    def _command(self, args: list[str]) -> list[str]:
        """Build the command vector to invoke MetricFlow.

        Invokes the Python entry-point (`dbt_metricflow.cli.main`) of the current
        interpreter when possible — this avoids relying on the `mf` shim being on
        PATH, which is brittle on Windows when launched from non-cmd shells.
        Falls back to the `mf` exe if the package is not importable but the shim
        happens to be on PATH (kept for someone who installs only the standalone
        binary).
        """
        try:
            import dbt_metricflow.cli.main  # noqa: F401
            return [sys.executable, "-m", "dbt_metricflow.cli.main", *args]
        except ImportError:
            exe = shutil.which("mf")
            if exe:
                return [exe, *args]
            raise MetricFlowError(
                "Neither the `dbt_metricflow` package nor the `mf` CLI is "
                "available. Install dbt-metricflow in this environment."
            )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        # PYTHONUTF8 avoids a Windows cp1252 crash in mf's spinner output.
        # Strip DBT_PROJECT_DIR — the agent's .env may set it to a path relative
        # to the repo root, but we pass cwd= to the dbt project explicitly, so
        # a stray relative env var would otherwise re-resolve against cwd inside
        # the child (e.g. "./warehouse" -> warehouse/warehouse/).
        env = {**os.environ, "PYTHONUTF8": "1"}
        env.pop("DBT_PROJECT_DIR", None)
        return subprocess.run(
            self._command(args),
            cwd=str(self.dbt_project_dir),
            capture_output=True,
            text=True,
            env=env,
        )

    def health_check(self) -> None:
        try:
            proc = self._run(["--help"])
        except (FileNotFoundError, MetricFlowError) as exc:
            raise MetricFlowError(
                f"MetricFlow CLI not available: {exc}"
            ) from exc
        if proc.returncode != 0:
            raise MetricFlowError(proc.stderr.strip() or "`mf --help` failed.")

    def explain(self, spec: QuerySpec) -> str:
        """Return the compiled SQL without touching the warehouse."""
        proc = self._run([*spec.to_mf_args(), "--explain"])
        if proc.returncode != 0:
            raise MetricFlowError(proc.stderr.strip() or proc.stdout.strip())
        return proc.stdout.strip()

    def run(self, spec: QuerySpec) -> list[dict]:
        """Execute the query against the warehouse and return rows as dicts."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "result.csv"
            proc = self._run([*spec.to_mf_args(), "--csv", str(csv_path)])
            if proc.returncode != 0:
                raise MetricFlowError(proc.stderr.strip() or proc.stdout.strip())
            if not csv_path.exists():
                return []
            with open(csv_path, newline="", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
