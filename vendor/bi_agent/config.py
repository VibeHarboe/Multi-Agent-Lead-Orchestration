from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    dbt_project_dir: Path
    planner_model: str
    max_agent_turns: int
    retrieval_k: int
    # The "today" the freshness and monitor checks compare against. Defaults to
    # the actual system date. Set AS_OF_DATE in .env to a fixed ISO date (e.g.
    # 2024-12-31 for the NordLedger simulation, whose data ends in 2024-12) so
    # freshness reports are meaningful against bounded simulation data.
    as_of_date: date | None

    @property
    def semantic_layer_dir(self) -> Path:
        return self.dbt_project_dir / "models" / "semantic_layer"

    @property
    def chroma_dir(self) -> Path:
        # The local Chroma index for metric retrieval — <repo root>/.chroma.
        return Path(__file__).resolve().parent.parent / ".chroma"


def load_config() -> Config:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in."
        )

    dbt_dir = os.environ.get("DBT_PROJECT_DIR")
    if not dbt_dir:
        raise RuntimeError(
            "DBT_PROJECT_DIR not set — point it at the ageras_kpi_dbt project root."
        )
    dbt_path = Path(dbt_dir).expanduser().resolve()
    if not (dbt_path / "dbt_project.yml").exists():
        raise RuntimeError(
            f"DBT_PROJECT_DIR does not look like a dbt project (no dbt_project.yml): {dbt_path}"
        )

    as_of_raw = os.environ.get("AS_OF_DATE", "").strip()
    as_of = date.fromisoformat(as_of_raw) if as_of_raw else None

    return Config(
        anthropic_api_key=api_key,
        dbt_project_dir=dbt_path,
        planner_model=os.environ.get("PLANNER_MODEL", "claude-sonnet-4-6"),
        max_agent_turns=int(os.environ.get("MAX_AGENT_TURNS", "6")),
        retrieval_k=int(os.environ.get("RETRIEVAL_K", "0")),
        as_of_date=as_of,
    )
