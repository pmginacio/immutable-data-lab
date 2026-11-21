# Projections

A design for reading the shape of a result, such as the rows an API endpoint returns, without writing its SQL.
Nothing is built yet.

## The problem

API backends often build their queries from SQL templates, with a piece of SQL for each optional filter and join.
The SQL text is spread over the templates, and each combination of filters is a different query that nobody wrote out.

inlay already removes most of that for records of one class.
Conditions are values, so optional filters are a list built in code and compiled once:

    conds = [Event.at >= start]
    if req.kind:
        conds.append(Event.kind == req.kind)
    if req.site:
        conds.append(Event.asset.site == req.site)
    events = db[Event].filter(*conds).sorted(key=Event.at, reverse=True)[:50]

A count of the events by a column chosen in the request is `aggregate_by`, with a dict of the allowed columns, so no request text reaches the SQL:

    GROUPABLE = {"kind": Event.kind, "site": Event.asset.site, "day": to_day(Event.at)}
    counts = db[Event].filter(*conds).aggregate_by(GROUPABLE[req.column], len)

What is missing is a result that is not a stored record: some fields of a record, with values from related records and computed ones.

## What a projection is

In relational algebra a selection picks rows and a projection picks columns; in SQL a projection is the list after `SELECT`.
ORMs use the word for a class or a query that reads part of a record, or a mix of related records: Spring Data's projections, Django's `values`, Entity Framework's `Select` into a DTO.

In event sourcing the word means something else: a table of state kept up to date from a log of events.
The current table of the datoms version is that kind of projection of the datoms.
This note is about the first kind.

## Design

    @projection(Event)
    class EventRow:
        id: str
        at: datetime
        asset_name: str = Event.asset.name
        site: str = Event.asset.site
        day: date = to_day(Event.at)

    rows = db[EventRow].filter(EventRow.site == "north").sorted(key=EventRow.at)[:50]
    rows = db[Event].filter(Event.kind == "alarm").project(EventRow)

- A projection is a frozen dataclass with one root class, an `@entity`.
- A field with the same name as a field of the root takes it; any other field names a value inlay can compile: a path through a `Ref`, a function, arithmetic.
- `@projection` checks each annotation against the type of its value when the class is defined, and raises `TypeError` if they differ.
- `db[EventRow]` is a read-only view: filters, sorting, slices, `len`, the aggregates and `aggregate_by` work on its fields, and it has no writes.
- `view.project(EventRow)` turns a view of the root, filtered on any of its fields, into a view of the projection; `db[EventRow]` is `db[Event].project(EventRow)`.
- A snapshot of the history db reads a projection at its transaction, since it reads the root there.

### The root

The root says what a row stands for: one row for each event.
Every other class is reached from it through `Ref`s, and a `Ref` points to one record, so each field has one value and the projection has as many rows as the root.
From an asset there are many events, so a projection rooted at `Asset` cannot have an event's field, only an aggregate over its events, which needs reverse relations.

The decorator could find the root by itself, since every value starts from it, but fields taken by name need it, and writing it first tells the reader the grain of the rows, as `FROM` does in SQL.

### Compiling

The first version compiles a projection as a subquery: the root's SELECT with one column for each field, and the rest of the compiler on top of it, as the datoms version read every class from a subquery without changing the compiler.

    SELECT ... FROM (
        SELECT e.id AS id, e.at AS at, (SELECT name FROM asset WHERE id = e.asset) AS asset_name, ...
        FROM event AS e
    ) WHERE site = ? ORDER BY at LIMIT 50

A condition on a field that comes through a `Ref` then sits on a correlated subquery, which cannot use an index of the related table.
Replacing each field of the projection with its value before compiling, so that `EventRow.site == "north"` becomes `Event.asset.site == "north"`, gives the `asset IN (SELECT id FROM asset WHERE site = ?)` form, which can.
That comes when filters on related fields turn out slow.

## Left out of the first version

- Several rows for one record of the root, such as an event for each of its tags: a join that multiplies rows changes what a row means.
- Values from the other side of a relation, such as the number of events of an asset, which needs reverse relations.
- Projections with one row per group; `aggregate_by` covers the common case.
- Empty values: a path through a `Ref` cannot be empty until inlay has optional fields, which real API data needs.

## Related roadmap items

- Optional fields, which should come first.
- Reverse relations, for projections rooted on the side with one record.
- Conditions on groups, several aggregates in one query, and the first N groups by their aggregate, for grouped endpoints.
