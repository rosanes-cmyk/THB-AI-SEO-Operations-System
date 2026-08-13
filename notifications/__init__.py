"""Notifications communicate. They never decide what is worth communicating.

The incident store decides; this package delivers. A delivery failure is
recorded and spooled — it never loses an incident and never stops the service.
"""
