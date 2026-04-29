from __future__ import annotations

import json
from typing import Any


def diversity_ratio(datasets: list[dict[str, list[dict[str, Any]]]]) -> float:
    if not datasets:
        return 0.0
    fingerprints = {json.dumps(dataset, sort_keys=True, default=str) for dataset in datasets}
    return len(fingerprints) / len(datasets)
