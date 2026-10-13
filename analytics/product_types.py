"""
Canonical Festool product categories.

The store's own `product_type` is inconsistent - it pluralises unpredictably
("Sander" vs "Sanders"), uses several names for the same thing ("Work Table" /
"Table" for an MFT), and is frequently just missing or "Unknown" on Recon
listings.  Grouping or filtering on the raw value therefore splits the same
category across several buckets.

This module normalises the raw value and, when there is nothing usable, infers
the category from the product title instead - every listing has a title, so a
category is always recoverable.  The ordering of the title rules matters: the
more specific pattern has to win, which is why Planex is tested before the
general sander rule (a PLANEX *is* a sander, but calling it one hides the most
expensive tool class on the site) and why Battery/Energy Set is tested first
(an "Energy Set" names the tools it charges).

Ported from the ReconToolBot web app's type-utils.js so the desktop app and
the site agree on what a "Sander" is.
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

UNKNOWN = 'Unknown'

# Every category this module can produce, for building filter lists.
CANONICAL_TYPES: List[str] = [
    'Battery', 'Drill', 'Edgebander', 'Grinder', 'Jigsaw', 'Joiner', 'Light',
    'MFT', 'Multitool', 'Planer', 'Planex', 'Radio', 'Router', 'Sander',
    'Saw', 'Systainer', 'Vac',
]

# Raw values that carry no information - treat them as "not set" and fall
# through to inferring the category from the title.
_EMPTY_TYPES = {'', 'unknown', 'other', 'none', 'n/a'}

# Ordered: first match wins, so specific patterns precede general ones.
_TITLE_RULES: List[Tuple[str, str]] = [
    (r'energy set|battery kit', 'Battery'),
    (r'planex|\blhs', 'Planex'),
    # The cordless models append a C (ETSC/RTSC/DTSC) and are followed by a
    # space before the size, so the corded "ETS 125" patterns miss them.
    (r'sander|rotex|\b(?:ets|rts|dts|ros)c\b|\bro\s*\d|\bets\s*\d|\bdts\s*\d'
     r'|\brts\s*\d|\brs\s*\d|\blex\s*\d', 'Sander'),
    (r'oscillat|vecturo|\bosc\b', 'Multitool'),
    (r'multifunction table|\bmft\b', 'MFT'),
    (r'jigsaw|carvex|\bpsbc|\bpsb\s', 'Jigsaw'),
    (r'grinder|renofix|\bagc\b', 'Grinder'),
    (r'drill|impact driver|impact drill|\bpdc\b|\btid\b|\btid\d|quadrive', 'Drill'),
    (r'saw|track saw|circular saw|\btsc\b|\bhkc\b|\brsc\b|\bkapex\b|\bts\s+\d', 'Saw'),
    # MFK is a router but is often listed without the word ("MFK 700 EQ/B-Plus")
    (r'\brouter\b|\bof\s+\d|\bmfk\b', 'Router'),
    (r'\bplaner\b|\bhl\s+\d', 'Planer'),
    (r'joiner|domino|\bdf\s+\d', 'Joiner'),
    (r'\bvac\b|dust extractor|\bct\s+\d|\bct\s+[a-z]', 'Vac'),
    (r'edgebander', 'Edgebander'),
    (r'\blight\b|syslite', 'Light'),
    (r'radio|sysrock', 'Radio'),
    (r'systainer|sys3', 'Systainer'),
]

_COMPILED_RULES = [(re.compile(pattern), name) for pattern, name in _TITLE_RULES]

# Raw store values that map onto a canonical name.
_TYPE_ALIASES: Dict[str, str] = {
    'sanders': 'Sander',
    'drills': 'Drill',
    'grinders': 'Grinder',
    'saws': 'Saw',
    'routers': 'Router',
    'work table': 'MFT',
    'table': 'MFT',
}

# Per-SKU corrections for the rare listing the rules get wrong.  Keep this
# small: a growing dictionary is a sign a title rule needs fixing instead.
TYPE_OVERRIDES: Dict[str, str] = {}


def guess_type(title: Optional[str]) -> str:
    """Infer a category from a product title, or UNKNOWN if nothing matches."""
    if not title:
        return UNKNOWN

    lowered = title.lower()
    for pattern, name in _COMPILED_RULES:
        if pattern.search(lowered):
            return name
    return UNKNOWN


def normalize_type(raw_type: Optional[str], title: Optional[str] = None) -> str:
    """
    Canonical category for a store-supplied product type.

    Falls back to the title when the store's value is missing or a placeholder,
    which on Recon listings is most of the time.
    """
    raw = (raw_type or '').strip()

    if raw.lower() in _EMPTY_TYPES:
        guessed = guess_type(title)
        return guessed if guessed != UNKNOWN else (raw or UNKNOWN)

    lowered = raw.lower()

    # A PLANEX is sold as a sander but is its own class of tool (and price).
    if lowered in ('sander', 'sanders') and title:
        if re.search(r'planex|\blhs', title.lower()):
            return 'Planex'

    return _TYPE_ALIASES.get(lowered, raw)


def resolve_type(item_number: Optional[str], raw_type: Optional[str],
                 title: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Final category for a listing.

    Returns (category, superseded) where superseded is the value the rules
    produced before a TYPE_OVERRIDES entry replaced it - None when no override
    applied.  Callers that show the category can surface the original so an
    override is visible rather than silently rewriting the store's data.
    """
    normalized = normalize_type(raw_type, title)

    if item_number and item_number in TYPE_OVERRIDES:
        return TYPE_OVERRIDES[item_number], normalized

    return normalized, None


def type_of(item_number: Optional[str], raw_type: Optional[str],
            title: Optional[str] = None) -> str:
    """resolve_type() when the caller only wants the category."""
    return resolve_type(item_number, raw_type, title)[0]


def summarize_types(items) -> List[Tuple[str, int]]:
    """
    Count listings per category, most common first.

    `items` is any iterable of objects carrying item_number / product_type /
    name attributes, which covers both Product and QueueObservation rows.
    """
    counts: Dict[str, int] = {}
    for item in items:
        category = type_of(
            getattr(item, 'item_number', None),
            getattr(item, 'product_type', None),
            getattr(item, 'name', None),
        )
        counts[category] = counts.get(category, 0) + 1

    # Unknown last regardless of size - it is a gap, not a category
    return sorted(
        counts.items(),
        key=lambda kv: (kv[0] == UNKNOWN, -kv[1], kv[0])
    )
