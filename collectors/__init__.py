"""Collectors gather evidence. They do not interpret it.

Every collector returns a `CollectorResult` whose status is one of ok /
unavailable / error, and whose findings are already normalized to the canonical
`Finding` schema. A collector never fabricates a value it could not measure
(Rule 7) and never raises past its own boundary (Rule 8).
"""

from collectors.base import CollectorResult, collector_guard

__all__ = ["CollectorResult", "collector_guard"]
