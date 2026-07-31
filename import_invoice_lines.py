"""Load itemized invoice lines from the Odoo export
'Journal Entry (account.move) (customer_invoices_itemized)'.

Layout (one row per invoice LINE):
  0 Invoice Partner Display Name   1 Invoice/Bill Date   2 Number
  3 Invoice lines/Product          4 Invoice lines/Quantity
  5 Invoice lines/Amount in Currency (journal-signed: sales negative)
  6 Total in Currency Signed       7 Total Signed
  8 Untaxed Amount Signed          9 Reference
Rows with a Number start an invoice; continuation rows carry only columns
3-5 and belong to the invoice above. Customer subtotal rows ("NAME (n)",
no number, no product) are skipped.

The import logic lives in services/sales_import.py (import_itemized) and is
shared with the Admin > Sales import screen, which auto-detects this export
by its header row. This script is the command-line wrapper.

Run:  SECRET_KEY=x python3 import_invoice_lines.py "<xlsx>"
"""
import sys

from app import create_app
from services.sales_import import import_itemized


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    app = create_app()
    with app.app_context():
        r = import_itemized(sys.argv[1])
        print(f"Invoices touched {r['read']} (headers created {r['inserted']}). "
              f"Lines loaded {r['matched_rows']}.")
        print(r["message"])
