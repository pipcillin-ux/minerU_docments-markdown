"""Reasoned sidecar construction and range algorithms."""

from .engine import (
    ancestor_effective_end_index,
    apply_decisions_for_document,
    apply_mode,
    block_index_by_id,
    build_reasoned_candidate_for_document,
    natural_node_end_indexes,
    recompute_reasoned_ranges,
)

__all__ = [
    "ancestor_effective_end_index",
    "apply_decisions_for_document",
    "apply_mode",
    "block_index_by_id",
    "build_reasoned_candidate_for_document",
    "natural_node_end_indexes",
    "recompute_reasoned_ranges",
]
