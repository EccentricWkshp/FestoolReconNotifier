"""
Build the printed-catalog index from the Festool catalog PDFs.

Run this once (and again whenever a new edition is added).  It is slow - the
catalogs are hundreds of large pages each - and completely offline: the app
never reads a PDF at runtime, only the table this writes.

    python build_catalog_index.py                     # every catalog found
    python build_catalog_index.py 2026-US.pdf         # just one edition
    python build_catalog_index.py --validate          # accuracy vs scraped prices
    python build_catalog_index.py --dry-run           # extract, store nothing

Extraction is heuristic (see catalog/extractor.py for why), so --validate is
the honest measure of whether the result is trustworthy: it compares extracted
list prices against the MSRPs the app has actually scraped from the store for
the same item numbers.
"""
import argparse
import logging
import sys
from pathlib import Path

from catalog.extractor import extract_catalog, find_catalogs, year_from_filename
from database.manager import DatabaseManager

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG_DIR = APP_DIR / 'ReconToolBot' / 'catalogs'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('build_catalog_index')


def validate(db: DatabaseManager) -> int:
    """
    Compare extracted list prices with MSRPs scraped from the store.

    The store's compare_at_price is the closest thing to ground truth we have.
    They are not required to agree - the catalog is a given year's list price
    and the store's figure is current - so a mismatch is not automatically an
    extraction error.  Exact matches are, though, strong evidence the pairing
    logic is sound.
    """
    from database.models import Product

    with db.get_session() as session:
        known = {
            row.item_number: (row.name, row.regular_price)
            for row in session.query(Product).filter(Product.regular_price.isnot(None)).all()
        }

    if not known:
        print("\nNo scraped MSRPs in the database to validate against.")
        return 0

    years = db.get_catalog_years()
    if not years:
        print("\nNo catalog index built yet - run without --validate first.")
        return 1

    print("\n" + "=" * 74)
    print("  Catalog index")
    print("=" * 74)
    print(f"  {'Edition':<10} {'Items':>8} {'Priced':>8} {'Coverage':>10}")
    for year, total, priced in years:
        pct = (priced / total * 100) if total else 0
        print(f"  {year:<10} {total:>8,} {priced:>8,} {pct:>9.0f}%")

    print("\n" + "=" * 74)
    print("  Accuracy against MSRPs scraped from the store")
    print("=" * 74)

    checked = exact = close = 0
    misses = []

    for item_number, (name, scraped) in sorted(known.items()):
        entry = db.get_catalog_entry(item_number)
        if entry is None or entry.msrp_usd is None:
            continue

        checked += 1
        delta = entry.msrp_usd - scraped
        if abs(delta) < 0.01:
            exact += 1
        elif abs(delta) / scraped <= 0.15:
            close += 1
        else:
            misses.append((item_number, name, scraped, entry))

    if not checked:
        print("  No overlap: none of the scraped items appear in the catalogs.")
        print("  That is itself a finding - Recon stock skews to discontinued models.")
        return 0

    print(f"  Items checked          : {checked}")
    print(f"  Exact price match      : {exact} ({exact / checked * 100:.0f}%)")
    print(f"  Within 15%             : {close} ({close / checked * 100:.0f}%)")
    print(f"  Differs by more        : {len(misses)} ({len(misses) / checked * 100:.0f}%)")

    if misses:
        print("\n  Largest differences (catalog year vs store's current MSRP):")
        misses.sort(key=lambda m: -abs(m[3].msrp_usd - m[2]))
        for item_number, name, scraped, entry in misses[:10]:
            print(f"    {item_number}  catalog {entry.catalog_year} "
                  f"${entry.msrp_usd:>9,.2f}  store ${scraped:>9,.2f}  "
                  f"[{entry.confidence}]  {(name or '')[:34]}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('pdfs', nargs='*',
                        help='specific catalog files (default: every PDF found)')
    parser.add_argument('--catalog-dir', default=str(DEFAULT_CATALOG_DIR),
                        help='where the catalog PDFs live')
    parser.add_argument('--db', default='festool_recon.db', help='database path')
    parser.add_argument('--max-pages', type=int, default=None,
                        help='stop after N pages per catalog (for a quick trial)')
    parser.add_argument('--bare-prices', choices=('auto', 'on', 'off'), default='auto',
                        help="read prices printed without a '$' (pre-2016 editions); "
                             "auto-detected per catalog by default")
    parser.add_argument('--dry-run', action='store_true',
                        help='extract and report, but write nothing')
    parser.add_argument('--validate', action='store_true',
                        help='report accuracy against scraped MSRPs and exit')
    args = parser.parse_args()

    db = DatabaseManager(db_path=args.db)

    if args.validate:
        return validate(db)

    catalog_dir = Path(args.catalog_dir)
    if args.pdfs:
        paths = []
        for name in args.pdfs:
            path = Path(name)
            if not path.exists():
                path = catalog_dir / name
            if not path.exists():
                logger.error(f"No such catalog: {name}")
                return 1
            paths.append(path)
    else:
        paths = find_catalogs(catalog_dir)

    if not paths:
        logger.error(f"No catalog PDFs found in {catalog_dir}")
        return 1

    logger.info(f"Indexing {len(paths)} catalog(s) from {catalog_dir}")

    total_items = 0
    for path in paths:
        year = year_from_filename(path)
        logger.info(f"--- {path.name} (edition {year}) ---")
        bare = {'auto': None, 'on': True, 'off': False}[args.bare_prices]
        try:
            items = extract_catalog(path, max_pages=args.max_pages, bare_prices=bare)
        except Exception as e:
            logger.error(f"{path.name}: extraction failed: {e}", exc_info=True)
            continue

        total_items += len(items)
        if args.dry_run:
            priced = [i for i in items if i.has_price]
            logger.info(f"{path.name}: dry run - {len(items)} item(s), "
                        f"{len(priced)} priced (nothing written)")
            for item in priced[:5]:
                logger.info(f"    {item.item_number}  ${item.msrp_usd:,.2f}  "
                            f"p{item.page}  [{item.confidence}]")
        else:
            db.replace_catalog_year(year, items, source_file=path.name)

    logger.info(f"Done: {total_items} catalog item(s) across {len(paths)} edition(s)")

    if not args.dry_run:
        validate(db)

    return 0


if __name__ == '__main__':
    sys.exit(main())
