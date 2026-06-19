"""Test fixtures for synthetic dataset generation."""

from .synthetic_data import (
    generate_balanced_binary,
    generate_imbalanced_binary,
    generate_multiclass,
)

__all__ = [
    'generate_balanced_binary',
    'generate_imbalanced_binary',
    'generate_multiclass',
]
