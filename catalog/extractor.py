"""
Pull item numbers and list prices out of the printed Festool US catalogs.

Why this is not simply "read the text": the catalogs are magazine layouts, not
tables.  A product card prints its order number and its price as separate
blocks, so on the page they belong together but in the text stream they land on
different lines with unrelated text between them.  Reading line by line pairs
the wrong price with the wrong tool.

What the pages actually look like, measured rather than assumed:

* Every price is labelled with its currency - "USD $269.00" above
  "CAD $375.00" - so the US price can be picked out reliably instead of
  guessing which of two numbers is which.
* On a product card the USD price sits at a small, consistent offset from the
  order number (on the 2026 pages, every one of the 13 items on a sample page
  had it ~9pt right and ~10pt below).  So the price is found by nearest
  neighbour in page coordinates, not by line order.
* The accessory and abrasive sections at the back *are* real tables, one item
  per printed row with USD and CAD side by side.  Those are unambiguous and
  are treated as the high-confidence case.
* Editions vary more than they look.  The pre-2016 catalogs print prices with
  no currency symbol at all, and some print the order number in two groups
  ("561 556"), so it never appears as a six-digit token.  Both are handled;
  neither is guessed at.

Nothing here is exact, so every price carries a `confidence` saying how it was
found, and `scripts` that load this data can insist on 'table' when it matters.
Run `python build_catalog_index.py --validate` to see measured accuracy against
prices the app has actually scraped from the store.

This module is deliberately import-light: pdfplumber is only needed to build
the index, never to run the app.
"""
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Festool US order numbers are six digits.
SKU_RE = re.compile(r'^\d{6}$')
MONEY_RE = re.compile(r'^\$[\d,]+\.\d{2}$')

# The pre-2016 editions print no currency symbol at all.  Their back-of-book
# index is a plain table - order number, description, page reference, price:
#
#     495368 Feeler Bearing set          83   36.30
#     490161 Flat sanding pad LS130     153   30.00
#
# so the price is a bare decimal.  Reading bare decimals as prices on a modern
# page would be a disaster ("1.76 lbs" is not a price), which is why this is
# only enabled for catalogs detected as old-format - see detect_bare_prices().
BARE_MONEY_RE = re.compile(r'^\d{1,3}(?:,\d{3})*\.\d{2}$')

# Some editions print the order number in two groups - "561 556" for 561556 -
# so it never appears as a single six-digit token.  The gap is typographic
# (~1.5pt, against ~9pt per group), far tighter than the space between two
# genuinely separate numbers, which is what keeps "230 240 V" from being read
# as an order number.
HALF_SKU_RE = re.compile(r'^\d{3}$')
MAX_SKU_SPLIT_GAP = 4.0

# Some pages render their text twice, one copy offset a hair from the other, so
# every character comes out duplicated: "1002" extracts as "110022".  Those
# doubled strings are the right length to pass for an order number.  They are
# recognisable because every adjacent pair of digits matches, and they recur
# across editions (the same publishing pipeline), so appearing in several
# catalogs is not evidence that one is real.
#
# The trade is deliberate: a genuine order number of the form aabbcc would be
# dropped too, which is roughly one in a thousand.  Measured against 149
# matches in the index - systematically 110000, 110011, 110022... - that is
# clearly the better side of the bargain.
DOUBLED_TEXT_RE = re.compile(r'^(\d)\1(\d)\2(\d)\3$')

# A currency label applies to the price immediately to its right on the same
# printed line.
CURRENCY_LABELS = ('USD', 'CAD')
LABEL_MAX_GAP = 60.0        # points between "USD" and the price it labels
LINE_TOLERANCE = 4.0        # points of vertical wobble still counted as one line

# How far from an order number a price may sit and still belong to it.  Deliberately
# tight: a card is roughly 55pt tall, so a wider window starts stealing the
# neighbouring card's price.
MAX_DX = 45.0
MAX_DY_BELOW = 40.0
MAX_DY_ABOVE = 18.0

# Prices outside this range are page furniture (dimensions, weights, part
# counts) rather than tool prices.
MIN_PRICE = 1.0
MAX_PRICE = 25000.0


@dataclass
class CatalogItem:
    """One order number as printed in one catalog."""
    item_number: str
    msrp_usd: Optional[float] = None
    msrp_cad: Optional[float] = None
    page: Optional[int] = None
    confidence: Optional[str] = None    # 'table' | 'proximity' | None

    @property
    def has_price(self) -> bool:
        return self.msrp_usd is not None


def is_order_number(text: str) -> bool:
    """A six-digit token that is a real order number rather than an artifact."""
    return bool(SKU_RE.match(text)) and not DOUBLED_TEXT_RE.match(text)


def year_from_filename(path) -> str:
    """'2011-12-US.pdf' -> '2011-12';  '2026-US.pdf' -> '2026'."""
    stem = Path(path).stem
    match = re.match(r'(\d{4}(?:-\d{2})?)', stem)
    return match.group(1) if match else stem


def _money(text: str) -> Optional[float]:
    try:
        return float(text.lstrip('$').replace(',', ''))
    except (TypeError, ValueError):
        return None


def _centre(word: dict) -> Tuple[float, float]:
    return ((word['x0'] + word['x1']) / 2.0, (word['top'] + word['bottom']) / 2.0)


def _group_lines(words: List[dict]) -> List[List[dict]]:
    """Words grouped into printed lines, each left-to-right."""
    rows: Dict[int, List[dict]] = {}
    for word in words:
        rows.setdefault(int(word['top'] / LINE_TOLERANCE), []).append(word)
    return [sorted(rows[key], key=lambda w: w['x0']) for key in sorted(rows)]


def _join_split_skus(lines: List[List[dict]]) -> List[dict]:
    """
    Recover order numbers printed as two three-digit groups.

    Returns synthetic word boxes spanning both halves, so everything
    downstream - line grouping, table rows, proximity pairing - treats them
    exactly like a normally-printed order number.
    """
    joined: List[dict] = []

    for line in lines:
        index = 0
        while index < len(line) - 1:
            left, right = line[index], line[index + 1]

            if (HALF_SKU_RE.match(left['text'])
                    and HALF_SKU_RE.match(right['text'])
                    and abs(right['top'] - left['top']) < 3.0
                    and 0 <= right['x0'] - left['x1'] <= MAX_SKU_SPLIT_GAP):
                joined.append({
                    'text': left['text'] + right['text'],
                    'x0': left['x0'],
                    'x1': right['x1'],
                    'top': min(left['top'], right['top']),
                    'bottom': max(left['bottom'], right['bottom']),
                })
                index += 2      # neither half can start another pairing
                continue

            index += 1

    return joined


def _label_currencies(lines: List[List[dict]]) -> Dict[int, str]:
    """
    Map id(word) -> 'USD'/'CAD' for every labelled price.

    A label claims the next price to its right on the same line, which is how
    the catalogs print them.
    """
    tagged: Dict[int, str] = {}
    for line in lines:
        pending: Optional[str] = None
        pending_x = 0.0
        for word in line:
            text = word['text']
            if text in CURRENCY_LABELS:
                pending, pending_x = text, word['x1']
                continue
            if pending and MONEY_RE.match(text):
                if word['x0'] - pending_x <= LABEL_MAX_GAP:
                    tagged[id(word)] = pending
                pending = None
    return tagged


def _table_rows(lines: List[List[dict]],
                tagged: Dict[int, str]) -> Dict[str, CatalogItem]:
    """
    Items printed as a table row: an order number and its prices on the same
    line.  Unambiguous, so these win over anything found by proximity.

    A row may carry several items side by side, which is how the 2024 pages
    print a product family:

        576451  $329.00      576991  $555.00

    Requiring one order number per row skipped those rows entirely.

    Unlabelled tables print USD first and CAD second (both are shown, and the
    CAD figure is always the larger), which is how the abrasive pages read.
    """
    found: Dict[str, CatalogItem] = {}

    for line in lines:
        skus = [w for w in line if is_order_number(w['text'])]
        if not skus:
            continue

        line_money = [w for w in line if MONEY_RE.match(w['text'])]
        if not line_money:
            continue

        for position, sku_word in enumerate(skus):
            if len(skus) == 1:
                # One item on the row: any price on it belongs to that item,
                # whichever side of the number it is printed on.
                money = line_money
            else:
                # Several items across one row - "576451 $329.00  576991
                # $555.00" is how the 2024 pages print a product family.  Each
                # order number takes the prices between it and the next one,
                # or the first one on the row would swallow them all.
                next_x = (skus[position + 1]['x0'] if position + 1 < len(skus)
                          else float('inf'))
                money = [w for w in line_money
                         if sku_word['x1'] <= w['x0'] < next_x]
                if not money:
                    continue

            item = _row_item(sku_word['text'], money, tagged)
            if item is not None:
                found[item.item_number] = item

    return found


def _row_item(item_number: str, money: List[dict],
              tagged: Dict[int, str]) -> Optional[CatalogItem]:
    """
    Build a table-row item from the prices printed alongside it.

    None when nothing usable is on the row, so the caller can leave the item
    to the proximity pass rather than recording a price it does not have.
    """
    usd = next((w for w in money if tagged.get(id(w)) == 'USD'), None)
    cad = next((w for w in money if tagged.get(id(w)) == 'CAD'), None)

    if usd is None and cad is None:
        # Unlabelled: left-to-right is USD then CAD
        usd = money[0]
        cad = money[1] if len(money) > 1 else None

    usd_value = _money(usd['text']) if usd is not None else None
    cad_value = _money(cad['text']) if cad is not None else None

    if usd_value is not None and not (MIN_PRICE <= usd_value <= MAX_PRICE):
        usd_value = None

    # A CAD figure below the USD one means the columns were misread
    if usd_value is not None and cad_value is not None and cad_value < usd_value:
        cad_value = None

    if usd_value is None:
        return None

    return CatalogItem(
        item_number=item_number,
        msrp_usd=usd_value,
        msrp_cad=cad_value,
        confidence='table',
    )


def _index_rows(lines: List[List[dict]]) -> Dict[str, CatalogItem]:
    """
    Old-format index rows: order number, description, page reference, price.

    A printed row often carries two items side by side, so each order number
    claims the first bare decimal that follows it and nothing further - the
    next order number starts a new pairing.  The page reference is a plain
    integer and cannot be mistaken for a price, which needs two decimal places.
    """
    found: Dict[str, CatalogItem] = {}

    for line in lines:
        pending: Optional[str] = None
        for word in line:
            text = word['text']

            if is_order_number(text):
                pending = text
                continue

            if pending and BARE_MONEY_RE.match(text):
                value = _money(text)
                if value is not None and MIN_PRICE <= value <= MAX_PRICE:
                    found[pending] = CatalogItem(
                        item_number=pending,
                        msrp_usd=value,
                        confidence='table',
                    )
                pending = None

    return found


def detect_bare_prices(pdf, sample_size: int = 20) -> bool:
    """
    True when a catalog prints prices without a currency symbol.

    Decided per catalog, never per page: a modern page that happens to show no
    price would otherwise start reading weights and dimensions ("1.76 lbs") as
    money.  Samples pages spread through the book, because the old editions
    keep all their prices in a back-of-book index.
    """
    total = len(pdf.pages)
    if not total:
        return False

    step = max(1, total // sample_size)
    for index in range(0, total, step):
        try:
            words = pdf.pages[index].extract_words(keep_blank_chars=False)
        except Exception:
            continue
        finally:
            try:
                pdf.pages[index].flush_cache()
            except Exception:
                pass

        if any(MONEY_RE.match(w['text']) for w in words):
            return False

    return True


def _nearest_price(sku_word: dict, priced: List[Tuple[dict, float]]) -> Optional[float]:
    """
    The price belonging to a product card's order number.

    Ranked by vertical distance first: cards stack vertically, so a price one
    row down is a different product, while a price a few points to the side is
    the same one.
    """
    sx, sy = _centre(sku_word)

    best: Optional[Tuple[float, float]] = None
    for word, value in priced:
        mx, my = _centre(word)
        dx, dy = mx - sx, my - sy

        if abs(dx) > MAX_DX:
            continue
        if not (-MAX_DY_ABOVE <= dy <= MAX_DY_BELOW):
            continue

        score = abs(dy) * 2.0 + abs(dx)
        if best is None or score < best[0]:
            best = (score, value)

    return best[1] if best else None


def extract_page(page, page_number: Optional[int] = None,
                 bare_prices: bool = False) -> List[CatalogItem]:
    """
    Every order number on one page, with its US list price where resolvable.

    Set bare_prices for the pre-2016 editions, which print no currency symbol
    (see detect_bare_prices - it is decided for the whole catalog, not here).
    """
    try:
        words = page.extract_words(keep_blank_chars=False)
    except Exception as e:
        logger.warning(f"Could not read words from page {page_number}: {e}")
        return []

    if not words:
        return []

    lines = _group_lines(words)

    # Order numbers split across two tokens have to be rebuilt before anything
    # else looks at the page, or they are invisible to every rule below.
    split_skus = _join_split_skus(lines)
    if split_skus:
        words = words + split_skus
        lines = _group_lines(words)

    tagged = _label_currencies(lines)

    # Editions that print "$585.00" with no USD/CAD label anywhere leave every
    # price unclassified, and proximity pairing would find nothing at all.
    # These are the US catalogs, so on a page that labels nothing an
    # unlabelled price is the US one.  A page that labels *some* prices is
    # telling us the unlabelled ones are something else, so this stays off.
    unlabelled_is_usd = not tagged

    # _table_rows has its own column-order fallback for unlabelled rows, so it
    # is given the labels exactly as printed.
    items = _index_rows(lines) if bare_prices else _table_rows(lines, tagged)

    # Everything else on the page, paired by position
    usd_priced: List[Tuple[dict, float]] = []
    cad_priced: List[Tuple[dict, float]] = []
    for word in words:
        if not MONEY_RE.match(word['text']):
            continue
        value = _money(word['text'])
        if value is None or not (MIN_PRICE <= value <= MAX_PRICE):
            continue
        currency = tagged.get(id(word))
        if currency == 'USD' or (currency is None and unlabelled_is_usd):
            usd_priced.append((word, value))
        elif currency == 'CAD':
            cad_priced.append((word, value))

    for word in words:
        text = word['text']
        if not is_order_number(text):
            continue

        # An order number is often printed more than once on a page - once in
        # the spec block that carries the price, and again in a caption or
        # cross-reference that does not.  Taking the first occurrence and
        # moving on loses the price whenever the bare mention comes first,
        # which is how 577619 ended up in the 2026 index with no price while
        # "USD $555.00" sat 13pt below its other occurrence.
        existing = items.get(text)
        if existing is not None and existing.msrp_usd is not None:
            continue

        usd_value = _nearest_price(word, usd_priced) if usd_priced else None
        cad_value = _nearest_price(word, cad_priced) if cad_priced else None

        if existing is not None and usd_value is None:
            continue

        items[text] = CatalogItem(
            item_number=text,
            msrp_usd=usd_value,
            msrp_cad=cad_value,
            confidence='proximity' if usd_value is not None else None,
        )

    for item in items.values():
        item.page = page_number

    return list(items.values())


def extract_catalog(pdf_path, progress_every: int = 25,
                    max_pages: Optional[int] = None,
                    bare_prices: Optional[bool] = None) -> List[CatalogItem]:
    """
    Every order number in one catalog, best price per item.

    An order number can be printed several times (a tool appears in its own
    section and again in an accessory list).  The best-evidenced price wins:
    a table row beats a proximity match, and among equals the first one found.
    """
    import pdfplumber  # imported here so the app never needs it

    pdf_path = Path(pdf_path)
    merged: Dict[str, CatalogItem] = {}
    rank = {'table': 2, 'proximity': 1, None: 0}

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        limit = min(total, max_pages) if max_pages else total

        if bare_prices is None:
            bare_prices = detect_bare_prices(pdf)

        style = "no currency symbol (old format)" if bare_prices else "USD/CAD labelled"
        logger.info(f"{pdf_path.name}: reading {limit} page(s) - prices {style}")

        for index in range(limit):
            try:
                items = extract_page(pdf.pages[index], page_number=index + 1,
                                     bare_prices=bare_prices)
            except Exception as e:
                logger.warning(f"{pdf_path.name} page {index + 1} failed: {e}")
                continue

            for item in items:
                current = merged.get(item.item_number)
                if current is None or rank[item.confidence] > rank[current.confidence]:
                    merged[item.item_number] = item

            if progress_every and (index + 1) % progress_every == 0:
                priced = sum(1 for i in merged.values() if i.has_price)
                logger.info(f"  {pdf_path.name}: page {index + 1}/{limit} - "
                            f"{len(merged)} item(s), {priced} priced")

            # Releases the page's cached objects; without this a 250MB catalog
            # climbs into gigabytes of resident memory.
            pdf.pages[index].flush_cache()

    priced = sum(1 for i in merged.values() if i.has_price)
    logger.info(f"{pdf_path.name}: {len(merged)} item(s), {priced} with a US price")
    return sorted(merged.values(), key=lambda i: i.item_number)


def find_catalogs(directory) -> List[Path]:
    """Every catalog PDF in a directory, oldest edition first."""
    return sorted(Path(directory).glob('*.pdf'), key=lambda p: year_from_filename(p))
