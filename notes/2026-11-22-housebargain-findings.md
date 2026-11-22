# What HouseBargain taught inlay

HouseBargain is a scraper of house listings on the Algarve that runs every night, with a FastAPI dashboard.
On 2026-11-22 its hand-written SQL storage was moved to inlay, on the `inlay` branch of its repo, as a first real use.
This note collects what that showed: what worked, and what inlay should get, with the evidence for each.

The app ended with these classes: `Sweep` (one scrape run), `Ad` (a listing's identity), `Sighting` (an ad seen in a sweep), `AdState`, `Location` and `Payload` (what the site showed, keyed by their values), and `Annotation` (the user's marks).
Its results, deals, export and dashboard data matched the SQL version on a copy of the live data.

## What worked

- No SQL is left in the app; the storage module went from 289 lines to 247, most of them class declarations.
- The passes (dedup, enrichment, deal scoring) take any iterable of records, so their tests build records in memory and the real run passes the db's.
- STRICT tables caught a real bug: one OLX price had cents, 259738.92, in an `int` field, which SQLite had stored for months without a word.
- `update` adding or replacing made `save` simple, and once the model only added records it needed no special case at all.
- Projections fit the CSV export and the record the passes read, `Current`, with values read through two `Ref`s such as `Sighting.state.location.parish`.
- Values of groups, added for this app, give the newest sighting of each ad in one query: 5,514 of 10,790 sightings in 35 ms.
- `hashed()` keys, also added for this app, store each state, location and payload once: 5,514 ads share 1,239 locations.

## Requirements

### Values of groups declared without a db

A projection class is defined when its module is imported, before any db is open, but a value of a group needs a db: `first = db[Sighting].aggregate_by(Sighting.ad, min, Sighting.observed_at)`.
So `Current` cannot have `first_seen: datetime | None = first[Sighting.ad]`, and the app stores `first_seen` on `Ad` instead, a copy of what the sightings already say.
A group value declared on the class, bound to a db when its query runs, would remove the copy, and also let `sorted` keys and conditions be written once at module level.

### Projection fields that hold a referenced record

The passes read every ad with the values of the state it shows and of that state's location.
The projection that gives them, `Current`, repeats each of those values as its own field, about twenty lines of `price: int | None = Ad.state.price`.
A field typed as an entity class, such as `state: AdState = Ad.state` and `location: Location = Ad.state.location`, could read all the columns of the referenced record in the same query and build it.
The passes would read `row.state.price`, the shape of the stored data, and type checkers would know every field without more code.

Decided against: a projection is a choice of the values a use needs, and a whole record in a field would bring every value of its class whether needed or not, like `SELECT t.*` in a SQL view.
A long projection is better answered by smaller projections for each use.

### A key that is also a `Ref`

`Annotation` holds the user's marks on an `Ad`, keyed by the ad's key, so that the dedup pass, which rewrites ads, never touches them.
Its key cannot be a `Ref[Ad]`, so nothing checks that the ad exists, and `Annotation.id.source` cannot be read.
One-to-one records that extend another one are a common shape; a key declared as `key(Ref[Ad])` or similar would cover it.

### Grouped fields, stored in the same table

A location is eight fields that belong together and could check themselves, but a class cannot hold another one, so they were first spread over the listing by hand.
The app now stores `Location` as its own record behind a `Ref`, keyed by its values, which works because locations are shared.
For values that are not shared, such as a price with its currency, a nested class stored as columns of the same table would keep the grouping without a join.

### JSON values

The raw payload is a dict, stored as a `str` of JSON and parsed again by every pass that reads it.
A `dict` field stored as JSON text, and maybe readable in queries with SQLite's JSON functions, would remove that.

### Enums and composite keys

Both were already on the roadmap, and the app needed both.
The property and transaction types are enums in the scrapers and `str` in storage.
A listing is identified by its source and its id on the source, packed into the key `"olx:675153072"`.

### Previous values in a sorted group

A price drop is a sighting whose state's price is lower than the previous sighting's, which needs the previous record in the ad's sorted group.
This is already on the roadmap as values of a record's place in its sorted group; HouseBargain is the first real use for it.

### Reverse relations

An ad's history is its sightings, reached as `db[Sighting].filter(Sighting.ad == ad)`; `Ad` cannot name them.
This works, so it is a convenience; it is on the roadmap as reverse relations.

### `migrate` of several classes

A `hashed()` key changes when its class gets a field, and the records that point to it must change with it: a new field on `Location` changes every `AdState` key, which changes every `Sighting`.
That needs the `migrate` of several classes in one step that is already on the roadmap.

## Defects found

- `x in view` compares whole records, and for anything that is not a record of the class it is `False`: `payload.id not in db[Payload]`, given a key by mistake, was always true.
  Passing something that is not a record of the class should raise a `TypeError`.
- A `Function` written twice disagreed: the export's `round2` rounds 770.625 to 770.63 in SQLite and to 770.62 in Python.
  It is evidence for the design question of running the Python version inside SQLite.

## Adoption costs

- inlay needs Python 3.15, so the app had to move from 3.12.
- The app depends on inlay as a path dependency that is not editable, because the nightly job runs from launchd, which macOS keeps out of `~/Documents`, where inlay is.
  Every change to inlay needs `uv sync --reinstall-package immutable-data-lab` in the app.
