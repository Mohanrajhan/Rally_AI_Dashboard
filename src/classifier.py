"""
Tag classification module.

Classifies a TestCase's tag list into one of:
    "AI-Assisted", "Manual", "Unclassified"

Design goals:
- Zero code changes needed when tag naming conventions evolve — everything
  is driven by config/config.yaml's `classification.categories` map.
- Case/whitespace/punctuation-insensitive matching, so "AI-Assisted",
  "ai assisted", "AI_ASSISTED" all normalize to the same key.
- A test case can carry multiple tags; if it has tags mapping to BOTH
  AI-Assisted and Manual (data-quality edge case), AI-Assisted wins by
  default (configurable) since that's the safer default for adoption
  reporting — flip `ai_wins_conflicts` to False to prefer Manual instead.
"""

from __future__ import annotations

import re
from typing import Iterable


def _normalize(tag: str) -> str:
    """Lowercase, collapse whitespace/hyphens/underscores to a single space."""
    t = tag.strip().lower()
    t = re.sub(r"[-_\s]+", " ", t)
    return t


class TagClassifier:
    UNCLASSIFIED = "Unclassified"

    def __init__(
        self,
        categories: dict[str, list[str]],
        count_unclassified_as_manual: bool = False,
        ai_wins_conflicts: bool = True,
    ):
        """
        categories: e.g. {
            "AI-Assisted": ["ai-assisted", "ai-generated", ...],
            "Manual": ["manual", "manually-created", ...],
        }
        """
        self._lookup: dict[str, str] = {}
        for category, patterns in categories.items():
            for p in patterns:
                self._lookup[_normalize(p)] = category

        self.categories = list(categories.keys())
        self.count_unclassified_as_manual = count_unclassified_as_manual
        self.ai_wins_conflicts = ai_wins_conflicts

        if "AI-Assisted" not in self.categories or "Manual" not in self.categories:
            raise ValueError(
                "classifier config must define at least 'AI-Assisted' and 'Manual' categories"
            )

    def classify_tag(self, tag: str) -> str | None:
        """Return the category a single tag maps to, or None if unmatched."""
        return self._lookup.get(_normalize(tag))

    def classify(self, tags: Iterable[str]) -> str:
        """Classify a test case given its full tag list -> one category label."""
        matched = {self.classify_tag(t) for t in tags}
        matched.discard(None)

        if not matched:
            return "Manual" if self.count_unclassified_as_manual else self.UNCLASSIFIED

        if len(matched) == 1:
            return matched.pop()

        # conflict: tags map to more than one category
        if "AI-Assisted" in matched:
            return "AI-Assisted" if self.ai_wins_conflicts else "Manual"
        # multiple non-AI categories matched — collapse to Manual
        return "Manual"

    def is_ai_assisted(self, tags: Iterable[str]) -> bool:
        return self.classify(tags) == "AI-Assisted"


def build_default_classifier() -> TagClassifier:
    """Convenience constructor wired to config/config.yaml via settings."""
    from src.settings import settings  # local import avoids circulars in tests

    return TagClassifier(
        categories=settings.tag_categories,
        count_unclassified_as_manual=settings.count_unclassified_as_manual,
    )
