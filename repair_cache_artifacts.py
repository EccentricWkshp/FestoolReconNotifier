"""
Find listings the stale front page fabricated, and repair the ones that can be.

Until the live product was resolved from the collection feed (see "The front
page lies" in the README), the app took the home page at its word.  That page
is served from a cache that can lag the store by hours, which left two marks on
the product log.  They need different treatment, and conflating them destroys
good data:

**Flap listings - repairable.**  Two cached copies of the page name different
products, so the app records A, then B a minute later, then A again.  B was
never live at all, and A gets counted as two listings at one price - which is
where the self-contradicting "best price seen (previous best <the same
number>)" came from.  The physical truth is a single unbroken run of A, so
that is what this rebuilds: B is deleted, and the two halves of A are merged
back into one listing spanning both.

**Frozen listings - reported only.**  A cached page names a product that is
still in stock but no longer at the front of the queue.  Nothing contradicts
it, so the app sits there for hours and records one enormous listing.  Here the
*start* was usually a real rotation and only the duration is wrong, so deleting
these would throw away true history to fix a bad number.  They are listed so
you know which durations to distrust, and left alone.

    python repair_cache_artifacts.py            # report only
    python repair_cache_artifacts.py --apply    # merge flaps, after a backup

Stop the monitor before running with --apply.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# A listing shorter than this, sandwiched between two listings of the product
# it interrupted, is the page flapping between caches rather than a fast sale.
FLAP_MINUTES = 10

# The store rotates every few hours.  A listing this long means the app stopped
# being told about rotations, not that one tool sold for a week.
FROZEN_HOURS = 24


def _parse(value):
    return datetime.fromisoformat(value) if value else None


def _load(conn):
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        "SELECT id, item_number, name, sale_price, posted_at, replaced_at, "
        "       minutes_live "
        "FROM products ORDER BY posted_at ASC, id ASC"
    ))


def _hours_live(row, now):
    posted = _parse(row['posted_at'])
    if not posted:
        return 0.0
    return ((_parse(row['replaced_at']) or now) - posted).total_seconds() / 3600.0


def find_flaps(rows):
    """
    [(phantom, first_half, second_half)] for each A -> B -> A interruption.

    The three rows are consecutive, so a flap that swallowed two products in a
    row is not matched - that is ambiguous enough to be worth a human look.
    """
    flaps = []
    for i in range(1, len(rows) - 1):
        before, phantom, after = rows[i - 1], rows[i], rows[i + 1]
        minutes = phantom['minutes_live']
        if (before['item_number'] == after['item_number']
                and before['item_number'] != phantom['item_number']
                and minutes is not None and minutes <= FLAP_MINUTES):
            flaps.append((phantom, before, after))
    return flaps


def find_frozen(rows, now, exclude_ids):
    """[(row, hours)] for listings too long to be a real run."""
    frozen = []
    for row in rows:
        if row['id'] in exclude_ids:
            continue
        hours = _hours_live(row, now)
        if hours >= FROZEN_HOURS:
            frozen.append((row, hours))
    return frozen


def _describe(row):
    return (f"#{row['id']}  {row['item_number']}  {row['name'][:44]}\n"
            f"        ${row['sale_price']:,.2f}  posted {row['posted_at']} UTC")


def main():
    parser = argparse.ArgumentParser(
        description="Report and repair listings left behind by the stale front page.")
    parser.add_argument('database', nargs='?', default='festool_recon.db')
    parser.add_argument('--apply', action='store_true',
                        help="merge the flap listings (a backup is taken first)")
    args = parser.parse_args()

    if not os.path.exists(args.database):
        print(f"Database not found: {args.database}")
        return 1

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = sqlite3.connect(args.database)
    rows = _load(conn)

    flaps = find_flaps(rows)
    merged_ids = {r['id'] for flap in flaps for r in flap}
    frozen = find_frozen(rows, now, merged_ids)

    print(f"{len(rows)} listings recorded.\n")

    if flaps:
        print(f"{len(flaps)} flap(s) - phantom listings that can be repaired:\n")
        for phantom, first, second in flaps:
            span = (_parse(second['replaced_at'] or second['posted_at'])
                    - _parse(first['posted_at']))
            print(f"  {_describe(phantom)}")
            print(f"        lasted {phantom['minutes_live']} min, interrupting "
                  f"{first['item_number']}")
            print(f"        -> delete #{phantom['id']}, merge #{first['id']} and "
                  f"#{second['id']} into one listing of "
                  f"{span.total_seconds() / 3600:.0f}h\n")
    else:
        print("No flap listings found.\n")

    if frozen:
        print(f"{len(frozen)} listing(s) longer than {FROZEN_HOURS}h - the app was "
              f"frozen on a cached page.\nThe start of each was probably a real "
              f"rotation, so these are reported, not touched:\n")
        for row, hours in frozen:
            print(f"  {_describe(row)}")
            print(f"        recorded as {hours:.0f}h live"
                  f"{' and still open' if not row['replaced_at'] else ''}"
                  f" - treat that duration as unknown\n")
        print("  If you know one of these was never actually on sale, delete it by\n"
              "  hand; nothing here can tell a missed rotation from a long one.\n")

    if not flaps:
        return 0

    if not args.apply:
        print("Report only - nothing was changed. Re-run with --apply to merge "
              "the flaps above.")
        return 0

    backup = f"{args.database}.pre_repair_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(args.database, backup)
    print(f"Backed up to {backup}")

    for phantom, first, second in flaps:
        end = second['replaced_at']
        minutes = None
        if end:
            minutes = int((_parse(end) - _parse(first['posted_at'])).total_seconds() / 60)
        conn.execute(
            "UPDATE products SET replaced_at = ?, minutes_live = ? WHERE id = ?",
            (end, minutes, first['id'])
        )
        conn.execute("DELETE FROM products WHERE id IN (?, ?)",
                     (phantom['id'], second['id']))

    conn.commit()
    print(f"Merged {len(flaps)} flap(s); {len(flaps) * 2} listing(s) removed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
