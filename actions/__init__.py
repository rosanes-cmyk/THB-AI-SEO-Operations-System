"""Actions execute. Policy decides whether they are allowed to.

Nothing in this package performs a production write unless every gate agrees:
the action's risk tier permits it, the adapter is explicitly enabled, an
approval is attached, and rollback data was captured first.
"""
