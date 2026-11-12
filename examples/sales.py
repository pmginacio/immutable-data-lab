"""Sales reports for a bar, from the SAF-T (PT) files its billing system exports.

Run it from the repository root:

    uv run python examples/sales.py import saft-2023*.xml
    uv run python examples/sales.py monthly
    uv run python examples/sales.py daily 2023 5
    uv run python examples/sales.py standing
    uv run python examples/sales.py hours --weeks 5

The data goes to `sales.db` in the current folder, or to the file given with `--db`.
Comments that start with "inlay:" point at a feature of the library.
"""

import argparse
import calendar
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import inlay
from inlay import Db, Ref, entity, key, to_month

# The bar closes after midnight, so a sale before this hour belongs to the day before.
DAY_STARTS_AT = timedelta(hours=6)

# ---------------------------------------------------------------------------
# The data
#
# inlay: money is stored in cents, so sums are exact.
# ---------------------------------------------------------------------------


@entity
class Product:
    id: str = key()  # the product code from the billing system
    name: str


@entity(indexes=[("at",)])
class Invoice:
    id: str = key()  # the document number, such as "FS 2023/1234"
    at: datetime
    total: int  # with tax; negative for a credit note


@entity(indexes=[("invoice",), ("product",)])
class Line:
    id: str = key()  # the document number and the line number
    invoice: Ref[Invoice]
    product: Ref[Product]
    quantity: float
    amount: int  # without tax; negative for a credit note


# ---------------------------------------------------------------------------
# Reading the files
#
# inlay: these functions only build records. They do not touch the db.
# ---------------------------------------------------------------------------


def read_saft(path: Path) -> tuple[list[Invoice], list[Line], dict[str, str]]:
    """The invoices and lines in a SAF-T file, and the description of each product code they use."""
    invoices, lines, names = [], [], {}
    for doc in ET.parse(path).getroot().iterfind(".//{*}SalesInvoices/{*}Invoice"):
        if doc.findtext("{*}DocumentStatus/{*}InvoiceStatus") == "A":  # cancelled
            continue
        sign = -1 if doc.findtext("{*}InvoiceType") == "NC" else 1
        number = _text(doc, "InvoiceNo")
        invoices.append(
            Invoice(
                id=number,
                at=datetime.fromisoformat(_text(doc, "SystemEntryDate")),
                total=sign * _cents(_text(doc, "DocumentTotals/{*}GrossTotal")),
            )
        )
        for row in doc.iterfind("{*}Line"):
            code = _text(row, "ProductCode")
            names[code] = _text(row, "Description")
            amount = row.findtext("{*}CreditAmount") or row.findtext("{*}DebitAmount") or "0"
            lines.append(
                Line(
                    id=f"{number}/{_text(row, 'LineNumber')}",
                    invoice=number,
                    product=code,
                    quantity=float(_text(row, "Quantity")),
                    amount=sign * _cents(amount),
                )
            )
    return invoices, lines, names


def _text(element: ET.Element, path: str) -> str:
    text = element.findtext("{*}" + path)
    if text is None:
        raise ValueError(f"{path} missing in {element.findtext('{*}InvoiceNo')}")
    return text


def _cents(amount: str) -> int:
    return round(Decimal(amount) * 100)


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------


def import_files(db: Db, paths: Sequence[Path]) -> None:
    for path in paths:
        invoices, lines, names = read_saft(path)
        products = [Product(id=code, name=name) for code, name in names.items()]
        # inlay: `update` replaces records with the same key, so importing a file twice changes nothing.
        # One call is one transaction: a file is imported completely or not at all.
        db.update(*products, *invoices, *lines)
        print(f"{path.name}: {len(invoices)} invoices, {len(lines)} lines")


# ---------------------------------------------------------------------------
# The reports
#
# inlay: `aggregate_by` asks the db for one number per group, such as the revenue of each month.
# Groups that inlay cannot compute, such as a business day that starts at 6:00, are built in Python
# from the records a view returns.
# ---------------------------------------------------------------------------


def monthly(db: Db) -> None:
    invoices = db[Invoice]
    # inlay: two queries for the whole table, and no invoice is loaded. The keys are dates, the first of each month.
    revenue = invoices.aggregate_by(to_month(Invoice.at), sum, Invoice.total)
    count = invoices.aggregate_by(to_month(Invoice.at), len)
    years = range(min(count).year, max(count).year + 1)

    def cell(month: date) -> str:
        return f"{money(revenue[month])} ({count[month]})" if month in count else ""

    rows = [[calendar.month_abbr[m], *(cell(date(y, m, 1)) for y in years)] for m in range(1, 13)]
    print_table(["Month", *map(str, years)], rows, "Revenue, with the number of invoices")


def daily(db: Db, year: int, month: int) -> None:
    start = datetime(year, month, 1) + DAY_STARTS_AT
    end = _next_month(datetime(year, month, 1)) + DAY_STARTS_AT
    revenue, count = Counter[date](), Counter[date]()
    # inlay: a business day starts at 6:00, which inlay cannot compute, so the invoices are grouped here.
    for invoice in db[Invoice].filter(Invoice.at >= start, Invoice.at < end):
        day = business_day(invoice.at)
        revenue[day] += invoice.total
        count[day] += 1
    rows = [[f"{day:%a %d}", money(revenue[day]), str(count[day])] for day in sorted(revenue)]
    print_table(["Day", "Revenue", "Invoices"], rows, f"{calendar.month_name[month]} {year}")


def standing(db: Db) -> None:
    invoices = db[Invoice]
    latest = invoices.max(Invoice.at)
    this_year, last_year = datetime(latest.year, 1, 1), datetime(latest.year - 1, 1, 1)
    same_point_last_year = _next_month(datetime(latest.year - 1, latest.month, 1))

    def revenue(start: datetime, end: datetime) -> int:
        return invoices.filter(Invoice.at >= start, Invoice.at < end).sum(Invoice.total)

    so_far = revenue(this_year, latest + timedelta(seconds=1))
    last_total = revenue(last_year, this_year)
    last_so_far = revenue(last_year, same_point_last_year)
    if not last_so_far:
        print(f"No sales up to the end of {calendar.month_name[latest.month]} {latest.year - 1} to compare with.")
        return
    # The rest of this year is assumed to follow last year's months.
    estimate = round(so_far * last_total / last_so_far)
    rows = [[money(last_total), money(so_far), money(estimate), f"{(estimate - last_total) / last_total:+.1%}"]]
    headers = [f"{latest.year - 1} total", f"{latest.year} so far", f"{latest.year} estimate", "Difference"]
    print_table(headers, rows, f"Up to {latest:%d %b %Y}")


def hours(db: Db, weeks: int) -> None:
    since = db[Invoice].max(Invoice.at) - timedelta(weeks=weeks)
    revenue = Counter[tuple[int, int]]()
    for invoice in db[Invoice].filter(Invoice.at >= since):
        revenue[invoice.at.hour, invoice.at.weekday()] += invoice.total
    shown = sorted({hour for hour, _ in revenue}, key=lambda h: (h - DAY_STARTS_AT.seconds // 3600) % 24)
    rows = [[f"{hour:02}h", *(money(revenue[hour, day] // weeks) for day in range(7))] for hour in shown]
    print_table(["Hour", *calendar.day_abbr], rows, f"Average revenue per week, last {weeks} weeks")


def business_day(at: datetime) -> date:
    return (at - DAY_STARTS_AT).date()


def _next_month(start: datetime) -> datetime:
    return (start + timedelta(days=32)).replace(day=1)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def money(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def print_table(headers: Sequence[str], rows: Iterable[Sequence[str]], title: str) -> None:
    rows = [list(headers), *map(list, rows)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    print(f"\n{title}\n")
    for n, row in enumerate(rows):
        first, *rest = row
        print("  ".join([first.ljust(widths[0]), *(cell.rjust(w) for cell, w in zip(rest, widths[1:]))]))
        if n == 0:
            print("  ".join("-" * w for w in widths))


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sales reports from SAF-T (PT) files.")
    parser.add_argument("--db", type=Path, default=Path("sales.db"), help="the database file (default: sales.db)")
    commands = parser.add_subparsers(dest="command", required=True)

    load = commands.add_parser("import", help="load SAF-T files into the db")
    load.add_argument("files", type=Path, nargs="+", help="SAF-T (PT) XML files")

    commands.add_parser("monthly", help="revenue per month, one column per year")
    day = commands.add_parser("daily", help="revenue per day in one month")
    day.add_argument("year", type=int)
    day.add_argument("month", type=int)
    commands.add_parser("standing", help="this year so far against last year")
    hour = commands.add_parser("hours", help="average revenue per hour and weekday")
    hour.add_argument("--weeks", type=int, default=5)

    args = parser.parse_args(argv)
    # inlay: the db is created on first use, and checked against the classes after that.
    with inlay.open(args.db, [Product, Invoice, Line]) as db:
        if args.command != "import" and not len(db[Invoice]):
            sys.exit(f"{args.db} has no invoices yet: run the import command first")
        match args.command:
            case "import":
                import_files(db, args.files)
            case "monthly":
                monthly(db)
            case "daily":
                daily(db, args.year, args.month)
            case "standing":
                standing(db)
            case "hours":
                hours(db, args.weeks)


if __name__ == "__main__":
    main()
