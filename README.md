# THB AI SEO Operations System

An always-on AI operations platform for Twin Home Buyer. It watches the pages
that produce seller leads, judges them the way a visitor actually sees them,
and prioritizes what it finds by **business and revenue impact** rather than
textbook SEO severity.

> A broken seller form on a money page is an emergency.
> A layout problem hiding a phone CTA on mobile is important.
> A duplicate title on an irrelevant old blog post is not.

This is not an SEO crawler with alerts bolted on. It is a monitoring and
remediation system whose ranking function is revenue.

---

## Status

**Stage 0 — Foundation. Built and tested. Not yet deployed.**

The master plan's Stage 1 is a baseline audit, which presupposes an existing
codebase. There wasn't one — this repository was empty — so this is the
foundation that Stage 1 audits and Stage 2 hardens.

| Capability | State |
|---|---|
| Revenue-page heartbeat (5 min) | Built, tested |
| AI visual recognition (Playwright + Claude) | Built, tested (needs `playwright install chromium`) |
| Technical crawl | Built, tested |
| PageSpeed | Built, tested (needs an API key, else reports *unavailable*) |
| Incident dedup / escalation / recovery | Built, tested |
| Google Chat notifications | Built, tested |
| Daily prioritized digest | Built, tested |
| Risk policy + audit journal + rollback | Built, tested |
| WordPress writes | **Disabled.** Four gates, all closed. |
| Search Console (Stage 4) | Built, tested (needs a service account, else *unavailable*) |
| GA4 (Stage 5) | Built, tested (needs a service account + property ID) |
| Revenue attribution / ROAS (Stage 6) | Built, tested (reads CSV exports; API adapters declared, not implemented) |
| Unified priority engine (Stage 7) | Built, tested |
| Approval control plane (Stage 9) | Not started |
| Local SEO (Stage 10) | Not started |
| Dashboard (Stage 13) | Shell built against real crawl data; not yet fed by Stages 4–7 |

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # optional; vision degrades without it

cp config.example.yaml config.yaml   # money pages + revenue weights
cp .env.example .env && chmod 600 .env

python main.py config-check          # validate configuration
python main.py selftest              # offline failure-injection checks
python main.py heartbeat             # one live money-page check
```

For production, run it under systemd — **not** from a terminal. See
[SYSTEMD.md](SYSTEMD.md).

---

## Commands

| Command | What it does |
|---|---|
| `config-check` | Validate `config.yaml`; report which credentials are present |
| `policy` | Print the machine-readable risk policy |
| `health` | Agent health, last success/failure, open incidents |
| `heartbeat` | One revenue-page check |
| `vision` | One visual inspection pass (`--no-ai` for evidence only) |
| `crawl` | One technical crawl (`--max-pages N`) |
| `pagespeed` | One PageSpeed pass |
| `search-console` | One Search Console pull (28-day window vs the prior 28) |
| `ga4` | One GA4 conversion pull |
| `revenue` | One revenue attribution / ROAS pass over the CSV exports |
| `board` | Print the priority board and send nothing (`--no-ai` for the deterministic ranking alone) |
| `digest` | Build and send the prioritized digest |
| `once` | Run every currently-due task once |
| `run` | The never-stop loop (systemd runs this) |
| `selftest` | Offline failure-injection checks; makes no network calls |

---

## Architecture

Strictly layered. The dependency arrow points inward, and nothing skips a layer.

```
                  runner.py                  never-stop loop, signals, dead-man
                      |
   +--------+---------+---------+----------+
   |        |         |         |          |
collectors  analysis  actions  notifications
   |        |         |         |
   +--------+---------+---------+
                      |
                    core/         schema, state, incidents, scheduling, policy
```

| Package | Responsibility |
|---|---|
| `core/` | Canonical `Finding`/`Incident` schema, atomic state, incident lifecycle, scheduler, URL normalization, retention, log redaction |
| `collectors/` | Gather evidence. Never interpret, never act |
| `analysis/` | Interpret evidence. Never gather, never act |
| `actions/` | Execute — only when policy, approval, and rollback data all agree |
| `notifications/` | Deliver. Never decide what is worth delivering |

Every agent normalizes what it observes into one `Finding` schema, so the
reasoning engine never sees a collector's raw shape. That is what stops Stage 7
from becoming a pile of special cases.

### The priority function

`Finding.priority_score()` is where the business principle lives:

```
score  = revenue_weight × 0.6  +  technical_severity × 6
score ×= 0.5 + confidence/2
score += 60 if conversion_blocking
```

Revenue weight dominates; technical severity is a tiebreaker. A
conversion-blocking defect gets a hard floor so it can never sort below a
cosmetic issue on a higher-weighted page. Deterministic scoring runs *first*,
and Claude reasons across those pre-computed signals — it never invents
priority from nothing.

### The priority board (Stage 7)

`analysis/priority_engine.py` reasons across all seven agents at once and does
three things a per-agent report cannot.

**It correlates.** When the vision agent sees a broken form on the seller page
and GA4 sees conversions collapse on that same page, those are not two
findings — they are one problem with a cause and a consequence. They merge
into a single item that names the pattern, and corroborated items outrank
isolated ones. Two guards: the *same* agent reporting twice is not
corroboration, and a genuine second defect from the leading agent stays its own
item rather than being absorbed.

**It buckets deterministically.** CRITICAL NOW / FIX NEXT / GROWTH
OPPORTUNITIES / MONITOR / NO ACTION are decided in plain Python, and every item
carries the reasons it landed where it did. Claude narrates the result; it
cannot change it. Any line the model returns that does not map to a real
finding is discarded, and a narration failure leaves the board intact.

**It reports what it could not see.** Coverage is computed from the collectors'
own statuses. A board built while three collectors were down says so in its
headline, before anything else — "nothing actionable found in what could be
checked" is a different statement from "all clear", and the system is not
allowed to confuse them.

```bash
python main.py board --no-ai      # the deterministic ranking, nothing sent
```

### Revenue attribution (Stage 6)

```
Channel Spend → Lead → Qualified Lead → Appointment → Offer
              → Contract → Closing → Gross Profit
```

Four rules govern what this agent will and will not say:

1. **Unattributed deals stay unattributed.** They are their own bucket, sorted
   last, never spread across channels. Redistributing unknowns inflates every
   channel at once and the inflation is invisible.
2. **No spend record means null ROAS** — not zero, not infinity — with the
   reason attached. A channel that cost something we did not record has an
   *unknown* return.
3. **Gross profit counts on closing only.** A projected margin on an open
   contract is a forecast, and forecasts do not belong in a ROAS numerator.
4. **Attribution quality gates the verdict.** Past the configured unknown-share
   threshold, the report says the numbers are unreliable *before* reporting
   them, and confidence scales with attribution quality — a last-touch guess
   never outranks a seller who said where they came from.

Drop `deals.csv` and `spend.csv` into `data/revenue/input/`. Column names are
matched loosely across CRM wordings; rows that cannot be parsed are reported
with a line number and reason, never dropped and never defaulted. Those files
contain seller records — `data/` is gitignored and must stay that way.

---

## Safety model

### Rule 7 — the system cannot silently guess

Collectors return `ok`, `unavailable`, or `error`. There is no code path that
returns a plausible-looking number that was not measured. No PageSpeed key
means "unavailable", not an estimate. A malformed Claude response means an
`unknown` verdict, not an invented finding.

### Rule 8 — one failure cannot cascade

Every task runs inside the scheduler's isolation boundary; a raising task is
logged, counted, backed off, and the loop continues. Vision failing does not
stop the heartbeat. Claude failing does not stop collection. Chat failing does
not lose an incident — undelivered messages spool to `data/chat_outbox.jsonl`.

### Rule 9 — no alert spam

Exactly three things may be said: **NEW INCIDENT**, **PERSISTENT / ESCALATED**,
**RECOVERED**. Findings are fingerprinted stably, so the same defect detected
every five minutes produces one NEW alert and then at most one PERSISTENT alert
per escalation window. Recovery is scoped to the agents that actually ran, so a
vision pass finding nothing cannot "recover" a heartbeat incident it never
checked.

### Rules 5 and 6 — tiered autonomy and rollback

`actions/policy.py` is the policy, as data:

- **AUTO** — retest, capture evidence, verify recovery, record incidents, send
  alerts, generate proposals. Nothing here touches production content.
- **APPROVAL** — titles, meta descriptions, internal links, alt text, schema.
  Prepared completely; executed only with an explicit approver.
- **HUMAN ONLY** — deletes, redirects, robots.txt, canonicals, Elementor
  structure, ranking-content rewrites, GBP identity, database writes. Never
  executed by this system under any circumstance.

A production write requires **all four** gates: the adapter's kill switch, the
risk tier, a recorded approver, and captured rollback data. A test asserts that
no production-write action is ever `AUTO`, and that unknown actions fail closed
to `HUMAN_ONLY`.

Every prepared, blocked, executed, verified, and rolled-back change is appended
to `data/audit_journal.jsonl` — who changed what, when, why, from what value,
to what value, and what happened after.

### Rule 11 — secrets

`.env` is gitignored and the pattern is committed. Logs pass through a
redaction filter that scrubs both known secret values and credential-shaped
strings, so a key cannot reach disk or journald even if a library echoes it
back. `Secrets.__repr__` lists which credentials are configured, never their
values.

---

## Tests

```bash
python -m pytest tests/ -q
```

136 tests, all passing. They assert behavior, not implementation:

- corrupt state recovers and is quarantined, not deleted
- a failing task does not stop a healthy one; backoff grows and is capped
- the 7 AM digest stays at 7 AM across a DST transition
- the same defect alerts once, escalates once per window, recovers once
- a conversion-blocking defect bypasses the persistence bar
- malformed Claude JSON yields `unknown`, never a fabricated finding
- a production write is blocked without rollback data, even when approved
- failed verification triggers rollback
- a DOM-present-but-invisible form is caught (the defect a heartbeat cannot see)

---

## Configuration

`config.yaml` (non-secret) sets pages, revenue weights, cadences, thresholds,
and limits. `.env` (secret, gitignored) holds credentials.

The single most important field is `revenue_weight` (0–100) on each page. It is
what makes the system prioritize by business impact. Set it honestly: a page
that produces contracts is 90+, an informational post is single digits.

---

## Known issue: bounded crawls can flap

`twinhomebuyer.com` has 669 pages. If `crawl_max_pages` is set below the real
page count, each run samples a *different* subset, so a finding present in one
run may simply not be sampled in the next — and because recovery is scoped by
agent, the incident store will read that absence as "recovered" and then
re-open it on the following run. That is exactly the alert churn Rule 9 exists
to prevent.

The shipped config sets `crawl_max_pages: 700`, which covers the whole site and
avoids this. Only lower it for local testing, and expect flapping if you do.
The durable fix is to scope crawl recovery to the URLs a run actually visited
rather than to the agent as a whole.

## What this does not do yet

It does not touch Search Console, GA4, revenue attribution, or Google Business
Profile. It does not write to WordPress. It cannot collect approvals — the
current Chat integration is a one-way incoming webhook, and building an
approval flow on top of that would be security theatre. Stage 9 needs a real
interactive control plane with authentication and authorization.
