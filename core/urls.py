"""URL normalization.

This is the join key. Search Console reports one URL shape, GA4 reports
another, the crawler reports a third, and WordPress reports a fourth. Landing
page datasets only merge if all four agree on what "the same page" means, so
normalization is defined once, here, and every agent uses it.

Deliberately conservative: it does not strip meaningful query parameters or
guess at canonicalization. Getting this wrong silently merges two pages'
metrics, which is worse than not merging at all.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking parameters that never identify a distinct page.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "ref",
        "referrer",
    }
)


def normalize_url(url: str, *, keep_query: bool = False) -> str:
    """Return a canonical form suitable for use as a cross-source join key.

    Applies, in order:
      * strip surrounding whitespace
      * lowercase scheme and host
      * force https (GSC and GA4 disagree constantly on protocol)
      * drop a leading `www.`
      * drop the default port
      * drop the fragment
      * drop tracking parameters, and all parameters unless `keep_query`
      * collapse the path to `/` when empty, and strip a trailing slash
        elsewhere

    Returns "" for input that is not a usable absolute or root-relative URL.
    """
    if not url or not isinstance(url, str):
        return ""

    raw = url.strip()
    if not raw:
        return ""

    # GSC sometimes emits bare "/path" rows; keep them addressable.
    if raw.startswith("/"):
        raw = f"https://placeholder.invalid{raw}"
        placeholder = True
    else:
        placeholder = False
        if "://" not in raw:
            raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""

    host = (parts.hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]

    # Non-default ports are meaningful; default ones are noise.
    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"

    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = ""
    if keep_query and parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        ]
        query = urlencode(sorted(kept))

    normalized = urlunsplit(("https", netloc, path, query, ""))
    if placeholder:
        normalized = normalized.replace("https://placeholder.invalid", "", 1) or "/"
    return normalized


def same_page(a: str, b: str) -> bool:
    """True when two URLs from different sources denote the same page."""
    left, right = normalize_url(a), normalize_url(b)
    return bool(left) and left == right


def is_same_site(url: str, base_url: str) -> bool:
    """True when `url` belongs to the same registrable host as `base_url`.

    Used by the crawler to stay on-site. Treats `www.` as equivalent, and any
    subdomain as a *different* site — a crawler that wanders into a subdomain
    is a crawler that blows through its page budget.
    """
    a, b = normalize_url(url), normalize_url(base_url)
    if not a or not b:
        return False
    return urlsplit(a).netloc == urlsplit(b).netloc
