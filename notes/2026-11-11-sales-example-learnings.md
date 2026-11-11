# What the sales example taught us about inlay

Notes from 2026-11-11, after writing `examples/sales.py`.
The example imports a bar's SAF-T (PT) sales exports and prints monthly, daily, hourly and year-to-date reports.
It was run on real data: 45 files, about 44,000 invoices and 140,000 lines.

## What worked well

- **Defining the data was short.**
  Three small classes, money in cents, `datetime` fields, and nothing else to configure.
- **Parsing stayed pure.**
  `read_saft` only builds records and never touches the db.
  The import is one `db.update(...)` call per file, so each file is imported completely or not at all.
- **Re-importing was free.**
  `update` replaces by key, so importing the same file twice changes nothing.
  Overlapping exports in the data folder were handled without any extra code.
- **The order of writes did not matter.**
  Products, invoices and lines go into one call in any order, because `Ref` fields are checked at commit.
- **Aggregates on date ranges are fast and read well.**
  `filter(Invoice.at >= start, Invoice.at < end).sum(Invoice.total)` per month took 29 ms for the whole monthly report.
  Loading every invoice and summing in Python took 124 ms, and that gap grows with the data, because each loaded row becomes a record with a parsed timestamp.
  `min` and `max` return a real `datetime`, which made the year and month arithmetic easy.
- **It handles real sizes.**
  All 45 files import in a few seconds.
- **Type checking helped.**
  Records are fully typed, and pyright caught a real mistake in the example.

## Where it got in the way

- **Grouping is the main gap.**
  Three of the four reports group by something computed: the business day, the hour, or the year and month.
  inlay cannot say "group by the month of `at`", so a report either loops in Python or runs one query per range, with its own date arithmetic (`_next_month`).
  Both are fine, but this is where SQL would have been shorter.
- **No joins.**
  A line has no date of its own, so a report per product and month needs a separate dict from invoice to date.
  Filtering through the relation (`Line.invoice.at >= since`) works well; combining fields of two tables in one result is the part written by hand.
- **No optional fields.**
  Real data has missing values.
  An unknown product family had to become a placeholder such as `"OTHER"`.
- **No composite keys.**
  A line's natural key is the invoice and the line number, and it had to be joined into one string, such as `"FS 2023/1/2"`.
- **Strict `Ref` fields cost a little code.**
  Every product a line refers to has to exist, so the import builds `Product` records from the line descriptions.
  That keeps the data consistent, but it is work that a looser store would not ask for.
- **Several numbers about one view need several queries.**
  Views are lazy, like any lazy iterable in Python, so the usual habit applies: do the work in one pass.
  (A view differs from a generator only in that reading it twice runs the query twice instead of returning nothing.)
  One pass in Python is possible: loop once and keep several totals.
  One pass in SQLite is not: `len(view)` and `view.sum(...)` are two queries, although SQLite could compute both in one `SELECT COUNT(*), SUM(...)`.
  The monthly report needs a count and a sum per month, so it runs two queries per month.
- **`update` never removes anything.**
  If a re-exported file no longer has a line, the old line stays in the db.
  Replacing everything a file covers needs a `clear(...)` first.

## Ideas for inlay, most useful first

1. **Optional fields (`X | None`).**
   The most common need with real data.
2. **`view.groupby(field)`, returning one view per value.**
   One `SELECT DISTINCT` for the values, then `len` and `sum` per group still run in SQLite.
   It does not cover computed keys such as "the month of a date", but it is cheap to add and fits the set model: splitting a set into subsets.
3. **Several aggregates in one query**, such as `view.summary(Invoice.total)` returning the count, sum, minimum and maximum together.
   It would make one SQLite pass enough for reports like the monthly one.
4. **Grouping by a computed key, such as the month of a date.**
   A bigger step: conditions and sorts would need expressions such as `month(Invoice.at)`, not only fields.
5. **Composite keys.**

## Grouping in plain Python

For reference, the idiomatic ways to group in Python, and when each fits:

- **A `Counter` or `defaultdict`**, filled in one loop.
  The default: short, and it works on input in any order.
- **`itertools.groupby` on sorted input.**
  It streams one group at a time, which matters when each group's records are needed or there are very many of them.
  The input must be sorted by the key, or the same key silently shows up as several groups; inlay can do that sort in SQLite with `sorted(key=...)`.
- **Asking SQLite for one number per group**, with one filtered view per group and `len` or `sum`.
  Best when the groups are ranges or values inlay can filter by, because no records are loaded.
