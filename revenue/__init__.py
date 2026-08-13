"""Stage 6 — revenue attribution.

The point of this package is to answer one question honestly: for every
dollar spent on a channel, how much gross profit came back?

Everything else in the system measures proxies — rankings, clicks, sessions,
form submissions. This is the only place that touches money, and it is
therefore the only place where a comforting guess does real damage. The rule
that governs every file here: **a deal whose source is unknown stays in the
unknown bucket.** It is never spread across channels to make the numbers add
up, because a redistributed unknown inflates every channel's ROAS at once and
the inflation is invisible.
"""
