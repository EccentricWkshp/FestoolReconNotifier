# Festool Recon Monitor

A cross-platform desktop application for monitoring [festoolrecon.com](https://www.festoolrecon.com/) for product changes and receiving instant notifications.

![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

### Core Functionality
- **Real-time Monitoring**: Automated checking at user-defined intervals (minimum 1 minute)
- **Multi-Channel Notifications** (sent on their own thread, so a slow or
  unreachable provider never freezes the window):
  - In-app / system-tray notifications
  - Email (SMTP)
  - SMS (Twilio - requires the optional `twilio` package)
  - Pushbullet
  - Webhooks (Discord, Slack, custom)
- **Two Selling Modes**: The store sells either one item at a time (rotating
  through a collection) or a whole collection at once. Which one is in force is
  read from the store on every check and drives what the rest of the app does -
  see [Selling modes](#selling-modes)
- **Look Ahead**: Reads the collection queue behind the live product, so you can
  see what is lined up next - with an honest record of how often that ordering
  actually holds. Only meaningful while the store is rotating; the tab says so
  when it is not
- **Next-item prediction**: The top of Look Ahead names the item most likely to
  go live next, how often the front of the queue has really been next, whether
  you will be notified about it, and how much longer the current item is likely
  to last - see [Next up](#next-up)
- **Collection View**: Every item on offer simultaneously during a multi-item
  sale, with price, MSRP, discount and price-check verdict per row.
  Double-click a name to open that exact listing on the store
- **Batch Alerts**: A sale that posts thirty items at once arrives as a single
  digest per channel, with watchlist matches called out - not thirty emails
- **Special Sale Detection**: Alerts when a collection other than the one being
  rotated appears on the store, and again if the store changes selling mode
- **Product History**: Complete tracking with time-live calculations
- **SKU History & Price Intelligence**:
  - Per-item rollup across every time it has been listed
  - Lowest / highest / current price and price trend per item
  - Discount off MSRP, per listing and catalog-wide
  - "Price check" verdict: is this the best price this item has ever been?
  - **Near-misses**: items the store queued and then pulled without ever
    listing them, counted per item and shown as their own rows, clearly marked
    - see [Queued and pulled](#queued-and-pulled)
- **Printed Catalog Cross-Reference**: Festool's own list price per year, taken
  from the 2011-2026 US catalogs, plus a discontinued-model signal for items
  that have dropped out of the current edition
- **Tool Categories**: Every listing is filed under a canonical category
  (Sander, Router, Saw, Domino/Joiner, Vac...), inferred from the title when the
  store does not supply one — so the Statistics tab can show what actually
  comes up for sale
- **Smart Filtering** - all on the **Filters** tab, see
  [What notifies you](#what-notifies-you):
  - Watch and exclude rules, by keyword or by item number, side by side
  - A plain-English summary of what currently notifies and where it is sent
  - A preview of every item ever seen: notifies or silent, and why
  - Per-item watch/exclude from the Look Ahead, SKU History **and Filters**
    tabs: right-click a row to watch it, exclude it, or clear its filter. SKU
    History reaches back over everything ever seen, so an item that is not in
    the queue today can still be set up now

### Additional Features
- **Data Export**: Export product history to CSV/Excel
- **Database Backup**: Automatic daily backups with easy restore
- **Statistics Dashboard**: Track monitoring performance and trends
- **System Tray Integration**: Minimize to tray for background monitoring
- **Configurable Notifications**: Include previous 1, 3, or 5 products with each alert

## Selling modes

festoolrecon.com does not always sell the same way, and nearly everything this
app reports depends on which way it is selling at the moment.

| | One at a time (`single`) | All at once (`multi`) |
|---|---|---|
| Front page | Renders one product, links no collections | Collection cards, no product template |
| What is live | The first available item in the collection | Every available item in the collection |
| Look Ahead | The queue behind the live item | Nothing - there is no queue |
| Collection tab | Empty, says the store is rotating | Every item on offer |
| Notifications | One per rotation | One digest per batch, plus watchlist hits |
| Recorded as | One `products` row per rotation | One row per item, opened when it appears and closed when it sells out |

**How the mode is decided.** From what the front page is doing, never from a
collection's name or item count. A one-at-a-time collection lists dozens of
available items too - that is its queue - so counts cannot tell the two apart.
The two shapes are cleanly distinguishable in the markup: a
`shopify-section-product-template` and no collection links at all, versus
collection cards and no product template. The collection holding the fronted
product is `single`; collections the front page is promoting are `multi`.

**When the front page cannot be read**, no mode is decided at all. That is a
third state, not a default, and nothing is recorded from a collection whose
mode is unknown. Guessing here is expensive in both directions: guess `multi`
and one bad request records an entire collection as new listings; guess
`single` and a live sale goes unnoticed.

**Nothing keys on a collection handle.** `oneanddone` ran alone for years and
then went empty the same afternoon the store opened a sale collection, so any
code that recognised a particular name would have been wrong that day. The
handle survives only as a fallback for the queue APIs on a fresh install.

**When the store stops rotating**, whatever was last up is closed out: it is
not live any more, and an open row would go on saying otherwise in History, in
the SKU rollup and on the Current Product tab, which blanks and says why
instead of leaving a listing that ended hours ago on screen as the current one.
The end time is stamped when it is noticed, but `minutes_live` is left NULL -
the store could have switched at any point since the last check, or while the
app was closed, and a duration measured from "now" would be invented and then
absorbed into every average. NULL is the "unknown" those averages already skip.
If the store resumes rotating with the same item, that is recorded as a new
listing rather than dismissed as no change.

## Queued and pulled

The look-ahead queue is a plan, and the plan does not always hold. Items get
lined up and then taken away again without ever being fronted. That record is
kept and surfaced in SKU History, because a near-miss is real information: it
says how often the queue's ordering is worth trusting, and an item pulled once
may well come round again.

Two columns carry it. **Times Listed** is how often the item was actually for
sale. **Queued, Pulled** is how often the store lined it up and changed its
mind. They are never added together — being queued is not being for sale, and a
single "how often have I seen this" number would quietly claim otherwise.

An item that has *only* ever been queued and pulled still gets a row, marked
**Never listed**: greyed, italic, and with a tooltip saying so. Its prices come
from the queue feed — what the store was advertising while the item waited — and
it has never been seen selling at any of them. Those rows are opt-in
(`get_sku_summaries(include_never_listed=True)`) and the SKU History tab is the
only thing that asks for them; every figure that counts SKUs or ranks the best
discounts means things that were genuinely for sale, and a tool nobody could
ever buy has no business among them.

**Notifications never block the window.** Every send is built on the GUI
thread - settings read, credentials decrypted, providers constructed, payload
formatted - and then handed to a single worker thread that does the part
talking to the network. One serialised queue, not a thread per send: order
carries meaning (a digest lands before the alerts it summarises), SMTP servers
dislike being opened several times at once, and a slow send must not pile up
threads behind it.

Every provider also carries its own deadline (30s; 10s for webhooks), because
a send with no timeout does not fail - it never returns. Pushbullet posts to
the documented v2 API directly rather than through the `pushbullet.py` client:
that client builds a bare session with no timeout and calls `refresh()` from
its own constructor, so merely *constructing* it made four blocking network
calls for devices, chats, channels and user info that a push with no named
recipient never uses. The one exception is the **Test** buttons in Settings,
which still send inline - the dialog is modal and you are waiting for the
answer - but they are now bounded by those same deadlines.

**"Time live" has three answers, and one of them is zero.** `minutes_live` is
NULL while a listing is still up, NULL again if it had to be closed without
evidence of how long it ran, and `0` when it is known and was under a minute -
which happens often, because things sell out that fast. Everything reading it
tests `is not None`; treating `0` as "no answer" quietly dropped the fastest
sell-outs out of the averages meant to describe them, and once had the
Statistics tab disagreeing with the database about the same figure.

**What a multi-item sale does to the statistics.** Its listings are recorded in
the same `products` table, tagged with `collection_handle`, so SKU rollups,
price history and the "best price ever" verdicts all see them - a price is a
price. But anything measuring the *rhythm* of the store leaves them out:
average time live, listings per week and busiest day are computed over the
rotation alone. Thirty items posted in one minute and left up for days is not
thirty rotations, and averaging it in would describe the sale instead of the
store.

## Installation

### Prerequisites
- Python 3.8 or higher
- pip (Python package manager)
- Virtual environment (recommended)

### Quick Start

1. **Clone or download the repository**
```bash
cd festool-recon-monitor
```

2. **Create a virtual environment**
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python3 -m venv venv
source venv/bin/activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Run the application**
```bash
python main.py
```

### Command line

```
python main.py [--monitor | --no-monitor]
```

| Flag | Effect |
|---|---|
| `--monitor` | Start monitoring as soon as the window opens |
| `--no-monitor` | Open without starting monitoring |
| *(neither)* | Use the **Start monitoring when the app opens** setting |

A flag applies to that launch only and is not written back to settings - a
one-off launch should not silently change what the app does every other time.
For a permanent choice, use the setting.

`--no-monitor` is not a remote control: it decides what the instance it starts
does, and cannot reach one that is already running. Anything Qt does not
recognise here is passed through to Qt untouched, so `-platform` and friends
still work.

To have monitoring start with Windows, point a shortcut or Task Scheduler entry
at `Start FRN.bat` and either tick the setting or add `--monitor` to the
command in that file.

## Project Structure

```
festool-recon-monitor/
├── main.py                 # Application entry point (PyQt6 UI)
├── requirements.txt        # Python dependencies
├── readme.md               # This file
├── migrate_database.py     # One-off timezone_mode column migration
├── repair_database_times.py# One-off UTC time repair (destructive - see below)
├── repair_cache_artifacts.py # Report/repair listings the stale front page invented
├── database/
│   ├── models.py           # SQLAlchemy database models
│   └── manager.py          # Database operations, backup/restore
├── scraper/
│   ├── festool_scraper.py  # Front-page product scraper + storefront mode
│   ├── collection_scraper.py # Look-ahead queue, collection index, mode rules
│   └── monitor.py          # Background monitoring service
├── notifications/
│   └── notifier.py         # Multi-channel notification system
├── build_catalog_index.py  # One-off: index the printed catalogs (offline)
├── analytics/
│   ├── sku_analytics.py    # SKU rollups, discounts, price verdicts
│   └── product_types.py    # Canonical tool categories
├── catalog/
│   └── extractor.py        # Pull SKUs + list prices out of catalog PDFs
├── utils/
│   ├── crypto.py           # Credential encryption at rest
│   ├── export.py           # CSV / JSON / Excel export and HTML reports
│   └── timezone_utils.py   # UTC storage -> local/UTC display
├── tests/
│   └── test_regression.py  # Headless regression suite (python tests/test_regression.py)
├── logs/                   # Rotating application logs
├── exports/                # Default export destination
└── backups/                # Auto-created backup directory
```

**Note:** `config.ini` in the project root is a leftover sample. The
application does **not** read it - all settings live in the database and are
edited through the Settings dialog.

## Configuration

### First Run
On first launch, the application creates:
- `festool_recon.db` - SQLite database
- `backups/` directory - For database backups
- `logs/festool_monitor.log` - Application logs (rotates at 2 MB, 5 kept)
- `.secret.key` - Key used to encrypt saved notification credentials

**Keep `.secret.key` next to the database and out of version control.** If it is
lost, saved SMTP passwords and API keys cannot be decrypted and must be
re-entered in Settings.

### Settings Configuration

1. **Monitoring Settings**
   - Check interval: 1-60 minutes (default: 5)
   - Auto-backup: Enable/disable daily backups

2. **Notification Settings**
   - **Email**: Configure SMTP server, port, credentials
   - **SMS**: Twilio API key and phone numbers
   - **Pushbullet**: API key from [pushbullet.com](https://www.pushbullet.com/)
   - **Webhook**: URL, method (POST/PUT/GET), and headers

## What notifies you

Every rule lives on the **Filters** tab of the main window. The Settings dialog
only decides *where* alerts go (email, SMS, Pushbullet, webhook), never *which*
products trigger them.

| Rule | Matches |
|---|---|
| Keyword | Any product whose name contains it (case-insensitive) |
| Item number | That one item only |

Type either into the Watch or Exclude box; something that looks like an item
number (a known one, or five or more digits) is stored as an item rule.
Right-clicking a row on Look Ahead, SKU History or the Filters preview does the
same for that item.

How the rules combine:

1. **Exclusions always win.** An excluded product never notifies - not as a
   live alert, not in a sale digest, not as a look-ahead warning, not as a
   desktop pop-up.
2. **An empty watchlist means "everything".** Every product that is not
   excluded notifies when it goes live.
3. **One watch rule switches to "watched only".** From then on only products
   matching a watch rule notify when they go live. The app asks before the
   first watch rule is added, because watching one item by right-click is easy
   to read as "and also this one".

| | Excluded | Watched | Neither, empty watchlist | Neither, watchlist in use |
|---|---|---|---|---|
| Goes live (one at a time) | Silent | Notifies | Notifies | Silent |
| Sale (many at once) | Left out | Own alert + digest line | Digest line | Digest line |
| Look-ahead warning | No | Yes, if switched on | No | No |

A new sale collection opening is always announced, whatever the filters.

The Filters preview runs every item through the same function the notifier
uses (`DatabaseManager.explain_item`), so what it says will notify is what
does.

> Before this tab existed, filters were split between Settings → Filters and
> the right-click menus, and saving Settings rebuilt the exclusion list from its
> keyword box - silently deleting every item excluded by right-click. Settings
> no longer touches filter rules at all.

## Usage Guide

### Starting Monitoring

1. Launch the application: `python main.py`
2. Click **Settings** to configure notification preferences
3. Set your desired check interval
4. Click **Start Monitoring**
5. The app will check festoolrecon.com at your specified interval

### Manual Check
Click **Check Now** to immediately check for product changes without waiting for the next scheduled check.

### Viewing History
- Navigate to the **History** tab
- View all tracked products with posting times and duration live
- Export to CSV for external analysis

### The front page lies

The obvious way to read this store is to scrape its home page. That is wrong,
and quietly so.

Shopify serves the home page from a **server-side full-page cache**. The
response carries an `ETag: W/"page_cache:<shop>:IndexController:<hash>"`, and
that cached copy can lag the real store by hours - long enough for several
products to sell out while the page still advertises the first of them.
Nothing sent from this end defeats it: `Cache-Control: no-cache`, a
cache-busting query string, a cookie-bearing session and byte-for-byte
browser-identical headers all return the same cached page with the same ETag.
The `ProductJson` blob embedded in that page is part of the same cache, so its
`available` flag is stale too and cannot be used to check itself.

**Asking whether the front page's product is sold out does not fix this**, and
that is worth spelling out because it is the trap this app fell into. The
reasoning looks sound - the store sells one tool at a time, so if the page is
stale the tool it names must be gone - and it holds right up until the store
rotates a tool *back into the queue* instead of retiring it. Then the cached
page names an item that is genuinely, verifiably in stock, every stock check
agrees, and the app sits on a product that stopped being live hours ago. That
happened: twenty hours frozen on a dust extractor that was sitting at position
3 of the queue the whole time, in stock, not for sale.

So stock answers the wrong question. The question is not *is this tool
available* but *is this the tool the store is selling right now*, and the
store answers that itself, in order:

- `/collections/oneanddone/products.json` lists the collection in the order the
  store rotates through it, so its **first entry is the live product**. The
  ordering is a property of the collection rather than of one cached page, and
  the JSON feed and the rendered collection page agree even while the home page
  does not.
- `/products/<handle>.js` reports real per-product stock, keyed per product
  rather than per page.

The front page is therefore a source of *detail*, not of identity. Its
description and image are used when it names the same item the feed puts
first, and ignored otherwise. The feed's leading entry is confirmed against
per-product stock before it is accepted, so a stale membership list cannot
hand back something already gone; if it is sold out, the next entry is tried.
Where the two sources disagree on price or SKU the feed wins - it is
regenerated when the store changes something, while the page may be a cached
copy of itself.

A failed stock check counts as *unknown*, never as *sold out*, so a network
blip cannot make the app discard a perfectly good scrape. If the feed cannot be
read at all the app falls back to the front page and says so in the log, which
is weaker than the normal path and is meant to be temporary. If nothing can
establish a live product, the check is skipped: recording nothing beats
recording a tool that is not on sale.

### Look Ahead

The front page shows one product at a time, but the collection behind it
(`/collections/oneanddone`) exposes the whole ordered queue. Sold-out entries
and the store's filler entry are dropped *before* positions are assigned, so
position 0 really is whatever is currently live and positions 1+ are what is
lined up behind it. That same position 0 is what the app records as the live
product, so the Current Product and Look Ahead tabs cannot disagree about what
is on sale - they did, while the front page was trusted for it. (Numbering before that filtering, as this once did, left
position 0 pointing at a tool that had already been and gone.) The
**Look Ahead** tab shows that queue, nearest first, with each
item's price, discount off MSRP, and a price check against its own history —
so an upcoming item at an all-time-low price is highlighted before it goes live.

The **In Queue** column is elapsed time since the item first appeared in the
queue, so it means the same thing whatever your check interval is. (It replaced
a "Seen In Queue" sighting count, which incremented once per look-ahead refresh
and so measured how often the app had checked rather than how long the item had
been waiting.)

**The queue is a plan, not a schedule.** Items get skipped, swapped, or sell out
almost immediately, so something listed here may never appear as the live
product. Rather than asking you to take the ordering on faith, the app measures
it: every queued item's outcome is recorded, and the tab reports the observed
record, e.g.

> Based on 23 recorded outcomes: 78% of items that went live came from the
> front of the queue; 22% of queued items left without ever going live.

Until enough outcomes accumulate it says so plainly instead of implying
confidence it has not earned.

#### Next up

The panel at the top of Look Ahead is the app's prediction, e.g.

> **Next up:** Router OF 1400 EQ-F-Plus (#576213) - $612.00 · you will be notified
> **How sure:** the front of the queue went live next 88% of the time (52 of 59
> recorded rotations). If not, the likeliest is #2: Domino Joiner DF 500 Q Set.
> **When:** the current item has been up 41m. Of the 212 past listings that
> lasted that long, half changed within another 1h 10m; the middle half between
> 25m and 3h 5m.

- **What** is simply the front of the store's own queue. Nothing the app could
  infer beats the store's published plan; what it adds is the measured record
  of how often that plan held.
- **When** is conditional on how long the current item has *already* been up:
  it looks only at past rotation listings that lasted at least that long and
  reports how much longer they went on (25th / 50th / 75th percentile). Sale
  listings are left out - thirty items up for days says nothing about one
  rotation. With fewer than five such listings it gives no estimate rather than
  a confident-looking guess. The panel refreshes every minute.
- It also says whether your filters will notify you about that item.

This replaced a Predictions tab whose "prediction" was the most common
category in recent history, with that category's share labelled as
confidence. It never named an item, and the queue made it redundant. The
`predictions` table and the two prediction columns in `user_settings` are left
in old databases, unused.

The store also keeps a placeholder entry ("Wow, that went fast!!!", SKU 999999)
in the collection as filler for sold-out stock. It is detected and excluded
rather than listed as an upcoming tool.

**Early warning:** if a watchlist keyword matches an item while it is still
queued, you get notified then — not after it is already live and possibly gone.
This requires at least one watchlist keyword; with an empty watchlist it would
fire for nearly every queued item, so it stays quiet instead. Toggle it on the
Filters tab.

**Special sales:** the collection index normally contains only `oneanddone`.
The app records every collection it sees and alerts if a different one appears,
which is the signal for a special sale.

### Printed Catalog Index

The store publishes its own "MSRP", which is the store marking its own
homework: a discount measured against it means only what the store wants it to
mean. Festool's printed US catalogs are an independent figure, and 2011-2026 of
them sit in `ReconToolBot/catalogs/`.

`build_catalog_index.py` reads them once, offline, and stores what it finds in
the `catalog_entries` table. The app never opens a PDF at runtime.

```bash
pip install pdfplumber            # only needed for this step
python build_catalog_index.py     # all editions (a few minutes)
python build_catalog_index.py 2026-US.pdf   # just one
python build_catalog_index.py --validate    # measured accuracy
```

Two things come out of it, shown on the Current Product panel and in the SKU
detail dialog:

- **List price** for the year — "2026 catalog list price: $269.00 — this
  listing is 23% below it"
- **Catalog membership** — an item that stopped being printed after 2019 is
  almost certainly a discontinued model, which for a refurbished-tool store is
  worth knowing before you buy.

**Extraction is heuristic and says so.** The catalogs are magazine layouts, not
tables: a product card prints its order number and price as separate blocks, so
reading line by line pairs the wrong price with the wrong tool. Prices are
matched by position on the page instead, and every row records how it was
found — `table` (same printed row, unambiguous) or `proximity` (adjacent on a
product card). Prices found by proximity are shown with an "approx." qualifier.
Run `--validate` to see extracted list prices checked against MSRPs the app has
actually scraped from the store.

**This is a price history independent of what the app has observed.** The index
holds every SKU printed in every edition — currently 3,129 distinct items over
14,846 rows, 14,242 of them carrying a list price — not just the handful the
app has caught on Recon. A tool that appears in the queue for the first time
already has its list-price history:

```text
201106   2017:$46  2018:$48  2020:$48  2021:$48  2022:$51
         2023:$54  2024:$54  2025:$54  2026:$40
```

**The editions do not share a format.** Three distinct layouts, each of which
yields almost nothing if you assume one of the others:

| Editions | Layout | Coverage |
| --- | --- | --- |
| 2019-2026 | Product cards, prices labelled `USD $x` / `CAD $x`; price sits ~9pt right and ~10pt below the order number | 94-99% |
| 2016-2018 | Same cards, plus rows carrying a whole product family side by side (`576451 $329.00  576991 $555.00`) | 91-100% |
| 2013-2015 | Order number printed as **two three-digit groups** (`561 556` = 561556, ~1.5pt apart), price *above* it, no currency labels | 98-100% |
| 2011-12 | Back-of-book index table, no currency symbol at all | 88% |

Measured against MSRPs actually scraped from the store, all 40 tracked items
were found in a catalog: 8 matched to the cent, 30 landed within 15%, and 2
differ by more. Some drift is expected and correct — the catalog is a given
year's list price, the store's figure is current — and the two large gaps were
checked against the printed page and are genuine, not extraction errors
(576887/576901 were cut from $249 to $199 in the 2026 edition while the store
still advertises an MSRP of $263.94, which is the sort of thing this
cross-reference exists to surface).

One artifact worth knowing about: some pages render their text twice, so
`1002` extracts as `110022` — six digits, indistinguishable from an order
number by length. These recur across editions (same publishing pipeline), so
appearing in several catalogs is *not* evidence a SKU is real. They are
rejected by pattern; the cost is that a genuine `aabbcc` order number would be
dropped too, roughly one in a thousand.

### SKU History

The **History** tab is a chronological event log - one row per time an item
appeared. The **SKU History** tab rolls that log up per item number, which is
what you need to answer "have I seen this before, and was it cheaper?"

Each row shows how many times an item has been listed, when it was first and
last seen, its lowest/highest/most recent price, the deepest discount off MSRP,
its average time live, and whether the price is trending up or down across
listings.

- **Filter** by name or item number with the search box
- **Repeats only** hides items seen just once
- **Click a column header** to sort (prices and dates sort by value, not text)
- **Double-click a row** to see every individual listing for that item
- **Export SKU History** writes the whole rollup to CSV

### Price Check

The **Current Product** panel shows a price verdict comparing the live listing
against that item's own history:

- *First time seen* - no prior data for this item number
- *Best price yet* - cheaper than it has ever been, with the size of the cut
- *Matches its best price* - as cheap as it has ever been, but not cheaper
- *$X (N%) above the best seen* - it has been cheaper before, by this much

**A tie is reported as a tie.** These tools recur at exactly the same price all
the time, and lumping "cheaper than ever" together with "the same as last time"
produced the self-contradicting *"Best price seen in 2 listings (previous best
$719.20)"* on a listing priced at $719.20. Only a genuine new low gets the
strong highlight, and a saving too small to round to a whole percent is quoted
in dollars alone rather than as "0% below".

The line underneath describes what the item has cost before - *"Previously seen
once, at $719.20"*, *"always at $719.20"*, or a real range - because an item
listed twice at one price was never priced *between $719.20 and $719.20*.

The same verdict is included in email, Pushbullet and SMS notifications.
Pushbullet titles are prefixed with **BEST PRICE** when an item hits a new low
and **TIES BEST PRICE** when it only matches one, so the distinction survives
into the notification preview.

### Statistics

The **Statistics** tab summarises the whole tracked catalog: unique items vs
total listings, repeat rate, years covered, listing cadence, busiest weekday,
median/average/deepest discount off MSRP, how long listings typically last, the
price mix, the most frequently listed items, and the deepest discounts on
record.

**Note on averages:** median and average time-live are both shown. If the app
is closed while an item is live, that item accrues "live" time until the app
next runs, which inflates the average. The median is the more reliable figure.

**Listings recorded before the front page was distrusted may be wrong.** While
the live product was taken from the home page, a cached copy could invent a
listing that never happened, or hold one open for days after it ended - both of
which distort times-listed, the low/high spread and every duration statistic.
`python repair_cache_artifacts.py` reports what it can find. It merges the
repairable cases (a listing split in two by a one-minute phantom) only when run
with `--apply`, and never deletes a long listing, because a missed rotation and
a genuinely long one look identical after the fact.

### Minimizing to Tray
Closing the window hides it to the system tray and monitoring keeps running.
To exit for real, use **File → Exit** (Ctrl+Q) or **Quit** from the tray menu.
If the system tray is unavailable on your desktop, closing the window exits
normally.

## Notification Configuration Examples

### Email (Gmail)
```
SMTP Server: smtp.gmail.com
Port: 587
Username: your.email@gmail.com
Password: your-app-password (not regular password!)
To: recipient@example.com
```

**Note**: For Gmail, you need to use an [App Password](https://support.google.com/accounts/answer/185833), not your regular password.

### SMS (Twilio)
1. Install the SMS dependency: `pip install twilio`
2. Sign up at [twilio.com](https://www.twilio.com/)
3. Get your Account SID (API Key) and Auth Token (API Secret)
4. Get a Twilio phone number
5. Configure in Settings:
   - Provider: Twilio
   - API Key: Your Account SID
   - API Secret: Your Auth Token
   - From Number: Your Twilio number (+1234567890)
   - To Number: Your phone number

### Pushbullet
1. Create account at [pushbullet.com](https://www.pushbullet.com/)
2. Go to Settings → Account → Create Access Token
3. Copy token and paste in Settings

### Webhook (Discord)
1. In Discord, go to Server Settings → Integrations → Webhooks
2. Create webhook and copy URL
3. In app settings:
   - URL: Your Discord webhook URL
   - Method: POST
   - Leave headers default

### Webhook (Slack)
1. Create a Slack app and enable Incoming Webhooks
2. Copy webhook URL
3. Configure in Settings (same as Discord)

## Database Management

### Backup
- Automatic backups run daily (configurable)
- Manual backup: File → Backup Database
- Backups stored in `backups/` directory
- Keeps last 10 backups automatically

### Restore
- File → Restore Database
- Select a backup file
- Confirm restoration (current database will be replaced)

### Database Location
Default location: `festool_recon.db` in application directory

To move database:
1. Stop monitoring
2. Move `festool_recon.db` file
3. Update `DatabaseManager` initialization in `main.py`

## Troubleshooting

### Common Issues

**Q: Application won't start**
- Ensure Python 3.8+ is installed: `python --version`
- Verify all dependencies installed: `pip install -r requirements.txt`
- Check logs: `logs/festool_monitor.log`
- A startup failure now shows a dialog with the error before exiting

**Q: Monitoring not detecting changes**
- Verify internet connection
- Check festoolrecon.com is accessible
- Increase check interval if getting rate limited
- Review logs for errors

**Q: Email notifications not working**
- Verify SMTP settings are correct
- For Gmail, use App Password, not regular password
- Check firewall isn't blocking port 587
- Test with "Check Now" to see immediate results

**Q: SMS not sending**
- Verify Twilio credentials are correct
- Ensure phone numbers include country code (+1 for US)
- Check Twilio account has credits
- Review logs for specific error messages

**Q: Database corrupted**
- Restore from backup: File → Restore Database
- If no backups, delete `festool_recon.db` (will lose history)

**Q: High CPU usage**
- Increase check interval in Settings
- Check logs for errors causing retries

### Debug Mode

Enable detailed logging by editing `main.py`:
```python
logging.basicConfig(
    level=logging.DEBUG,  # Change from INFO to DEBUG
    ...
)
```

## Advanced Features

### Custom Webhooks

Send notifications to any service accepting JSON POST requests:

**Example payload structure:**
```json
{
  "product": {
    "name": "Miter Saw KAPEX KS 120 REB",
    "item_number": "575306",
    "sale_price": 1440.75,
    "regular_price": 1800.94,
    "posted_at": "2025-10-11T10:30:00"
  },
  "previous_products": [...],
  "timestamp": "2025-10-11T10:30:00"
}
```

### Export Data

Choose the format from the file-type dropdown in the export dialog:

- **CSV**: Compatible with Excel, Google Sheets
- **Excel (.xlsx)**: Auto-sized columns, single "Products" sheet
- **JSON**: Full records including description and image URL

All formats include: item number, name, sale/regular price, posted and
replaced timestamps, and time live.

## Security Considerations

- Passwords and API keys are encrypted at rest (Fernet) using the key in
  `.secret.key`; the database alone does not reveal them
- The key file is only as protected as the account it lives under - anyone who
  can read both the key and the database can read the credentials
- Database backups contain the same encrypted values
- Keep `festool_recon.db` and `.secret.key` secure - together they contain
  sensitive data
- The bundled "default" user is a placeholder; there is no real login gate, so
  do not treat this as multi-user software

## Performance Notes

### Database
- SQLite with WAL mode for better concurrency
- Backups use SQLite's online backup API and are vacuumed on write
- Single shared connection (StaticPool), serialised across threads

### Monitoring
- Efficient HTML parsing with BeautifulSoup4 + lxml
- A fresh HTTP session per check. This does *not* defeat the store's caching
  on its own - see "The front page lies" below - so the live product is
  confirmed against real stock rather than taken on trust
- Respectful rate limiting (minimum 1 minute)
- Suspected changes are re-confirmed before being recorded, to filter out
  cache-induced false positives

### Notifications
- Sent on the monitoring thread, sequentially, after a change is confirmed
- Every attempt is logged to `notification_logs` for debugging
- A failing provider is logged and skipped; it does not block the others

## Running the Tests

A headless regression suite covers the database layer, notification
serialisation, credential encryption, SKU analytics and date formatting. It
uses a temporary database and never touches `festool_recon.db`:

```bash
python tests/test_regression.py
```

## Contributing

This is a personal project, but improvements are welcome:

1. Run `python tests/test_regression.py` before submitting changes
2. Follow PEP 8 style guidelines
3. Add comments for complex logic
4. Update README for new features

## Roadmap

Future enhancements under consideration:

- [x] Price tracking and history per item (SKU History tab)
- [x] Advanced search and filtering in history
- [ ] Charting and visualization of trends (data is available via
      `SkuAnalytics.get_listings_by_month()`)
- [x] Look-ahead at the upcoming product queue
- [ ] Watchlist alerts keyed to a target price ("tell me when this drops below X")
- [ ] Multi-user support with individual accounts
- [ ] Web interface for remote monitoring
- [ ] Mobile push notifications (native apps)
- [ ] Integration with more notification services
- [ ] Cloud database sync option

## License

MIT License - see LICENSE file for details

## Disclaimer

This application is for personal use only. Please respect festoolrecon.com's terms of service:
- Use reasonable check intervals (recommended: 5+ minutes)
- Do not overload their servers
- This tool is not affiliated with Festool or festoolrecon.com

## Support

For issues or questions:
1. Check this README and troubleshooting section
2. Review application logs: `logs/festool_monitor.log`
3. Ensure all dependencies are up to date: `pip install -r requirements.txt --upgrade`

## Acknowledgments

Built with:
- PyQt6 - Cross-platform GUI framework
- SQLAlchemy - Database ORM
- BeautifulSoup4 - HTML parsing
- APScheduler - Job scheduling

---

**Version**: 1.0.0  
**Last Updated**: October 2025