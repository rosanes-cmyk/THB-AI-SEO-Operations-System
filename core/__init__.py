"""Shared primitives for the THB AI SEO Operations System.

Layering rule (Rule 3): collectors collect, analyzers analyze, policies decide
risk, actions execute, verification verifies, notifications communicate, and
this package holds the state/schema/scheduling plumbing they all share.

Nothing in `core` may import from `collectors`, `analysis`, `actions`, or
`notifications` — the dependency arrow points inward only.
"""
