import runpy
from pathlib import Path

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_shop_example_runs():
    runpy.run_path(str(EXAMPLES / "shop.py"), run_name="__main__")


def saft(invoices: list[tuple[str, str, str, str, list[tuple[str, str, str, str]]]]) -> str:
    """A minimal SAF-T (PT) file: (number, type, timestamp, gross total, [(code, description, quantity, amount)])."""
    docs = []
    for number, kind, at, total, lines in invoices:
        amount_tag = "DebitAmount" if kind == "NC" else "CreditAmount"
        rows = "".join(
            f"<Line><LineNumber>{n}</LineNumber><ProductCode>{code}</ProductCode><Description>{desc}</Description>"
            f"<Quantity>{qty}</Quantity><{amount_tag}>{amount}</{amount_tag}></Line>"
            for n, (code, desc, qty, amount) in enumerate(lines, 1)
        )
        docs.append(
            f"<Invoice><InvoiceNo>{number}</InvoiceNo><DocumentStatus><InvoiceStatus>N</InvoiceStatus></DocumentStatus>"
            f"<InvoiceType>{kind}</InvoiceType><SystemEntryDate>{at}</SystemEntryDate>{rows}"
            f"<DocumentTotals><GrossTotal>{total}</GrossTotal></DocumentTotals></Invoice>"
        )
    return (
        '<AuditFile xmlns="urn:OECD:StandardAuditFile-Tax:PT_1.04_01"><SourceDocuments><SalesInvoices>'
        + "".join(docs)
        + "</SalesInvoices></SourceDocuments></AuditFile>"
    )


def test_sales_example_imports_and_reports(tmp_path, capsys):
    sales = runpy.run_path(str(EXAMPLES / "sales.py"))
    (tmp_path / "2022.xml").write_text(
        saft([("FS 2022/1", "FS", "2022-05-03T21:00:00", "10.00", [("1", "Pint", "2", "8.13")])])
    )
    (tmp_path / "2023.xml").write_text(
        saft(
            [
                ("FS 2023/1", "FS", "2023-05-04T01:30:00", "12.30", [("1", "Pint", "2", "8.13"), ("2", "Tremoços", "1", "1.87")]),
                ("NC 2023/1", "NC", "2023-05-05T20:00:00", "2.30", [("2", "Tremoços", "1", "1.87")]),
            ]
        )
    )
    db = ["--db", str(tmp_path / "sales.db")]
    files = [str(tmp_path / "2022.xml"), str(tmp_path / "2023.xml")]
    for _ in range(2):  # importing the same files again changes nothing
        sales["main"]([*db, "import", *files])
    for report in (["monthly"], ["daily", "2023", "5"], ["standing"], ["hours"]):
        sales["main"]([*db, *report])
    out = capsys.readouterr().out
    assert "10.00 (2)" in out  # May 2023: 12.30 minus the 2.30 credit note, over two documents
    assert "Wed 03" in out  # 01:30 on the 4th belongs to the 3rd
