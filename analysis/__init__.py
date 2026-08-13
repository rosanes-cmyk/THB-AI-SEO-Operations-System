"""Analyzers interpret evidence. They never gather it and never act on it.

Rule 7 is the hard constraint in this package: when Claude is unavailable,
returns malformed JSON, or refuses, the analyzer returns an `unknown` verdict.
It does not invent a finding, and it does not raise into the caller.
"""
