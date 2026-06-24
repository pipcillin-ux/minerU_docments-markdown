"""Section reasoning package with stable public range APIs."""

from .apply import (
    ancestor_effective_end_index,
    apply_decisions_for_document,
    block_index_by_id,
    build_reasoned_candidate_for_document,
    natural_node_end_indexes,
    recompute_reasoned_ranges,
)
from .cli import main
from .collect import collect_candidates_for_document

__all__ = [
    "ancestor_effective_end_index",
    "apply_decisions_for_document",
    "block_index_by_id",
    "build_reasoned_candidate_for_document",
    "collect_candidates_for_document",
    "main",
    "natural_node_end_indexes",
    "recompute_reasoned_ranges",
]
