"""Visualization spec — the declarative chart shape the explanation agent emits.

The agent never writes plotting code. It ends its answer with a fenced ```viz
block of JSON describing the chart shape; the interface (the CLI now, Streamlit
next) renders it. This module extracts and loosely validates that spec.
"""

from __future__ import annotations

import json
import re

# Matches a fenced ```viz { ... } ``` block in the agent's final answer.
_VIZ_BLOCK = re.compile(r"```viz\s*(\{.*?\})\s*```", re.DOTALL)

CHART_TYPES = ("bar", "line", "table", "none")


def extract_visualization(text: str) -> tuple[str, dict | None]:
    """Split an agent answer into (narrative, viz_spec).

    Returns the answer text with the ```viz block stripped out, plus the parsed
    spec — or None if there is no valid block. A missing or malformed spec is
    not an error: the narrative answer still stands on its own.
    """
    match = _VIZ_BLOCK.search(text)
    if not match:
        return text.strip(), None

    narrative = (text[: match.start()] + text[match.end():]).strip()
    try:
        spec = json.loads(match.group(1))
    except json.JSONDecodeError:
        return narrative, None
    if not isinstance(spec, dict) or spec.get("chart_type") not in CHART_TYPES:
        return narrative, None
    return narrative, spec
