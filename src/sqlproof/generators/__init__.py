from __future__ import annotations

from sqlproof.generators.columns import strategy_for_column, strategy_for_type
from sqlproof.generators.graph import Dataset, dataset_strategy
from sqlproof.generators.well_known import emails, phone_numbers, postal_codes, slugs, urls

__all__ = [
    "Dataset",
    "dataset_strategy",
    "emails",
    "phone_numbers",
    "postal_codes",
    "slugs",
    "strategy_for_column",
    "strategy_for_type",
    "urls",
]
