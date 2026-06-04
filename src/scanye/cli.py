import argparse
import json
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

from .client import ScanyeClient
from .exceptions import ScanyeError

CONFIG_DIR = Path.home() / ".config" / "scanye"
CONFIG_FILE = CONFIG_DIR / "config.json"


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        if isinstance(config, dict):
            return config
        return {}


def handle_login(args: argparse.Namespace) -> None:
    email = args.email
    password = getpass(f"Password for {email}: ")

    client = ScanyeClient(debug=args.debug)
    try:
        token = client.login(email, password)
        save_config({"token": token, "email": email})
        print("Login successful. Token saved.")
    except ScanyeError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)


def handle_invoices_list(args: argparse.Namespace) -> None:
    config = load_config()
    token = config.get("token")
    if not token:
        print("Not logged in. Run 'scanye login' first.", file=sys.stderr)
        sys.exit(1)

    is_sales = args.type == "sales"
    client = ScanyeClient(token=token, debug=args.debug)

    # Handle months filtering
    months = args.month
    filters = ["dateAuthenticated?isNotNull"]
    if args.unsent:
        filters.append("annotations.ksef.status?isNull")

    if args.filter:
        filters.append(args.filter)
    else:
        now = datetime.now()
        if not months:
            # Default to a broad range for current year to show everything relevant
            start_month = f"{now.year}-01"
            end_month = f"{now.year + 1}-01"
            filters.append(f"annotations.accountingMonth>={start_month}")
            filters.append(f"annotations.accountingMonth<={end_month}")
        else:
            # Use specific range for requested months
            # For simplicity, if multiple months are provided, we just use the range from min to max
            sorted_months = sorted(months)
            filters.append(f"annotations.accountingMonth>={sorted_months[0]}")
            filters.append(f"annotations.accountingMonth<={sorted_months[-1]}")

    try:
        # Fetch more if we are filtering client-side
        fetch_limit = args.limit * 5 if args.unsent else args.limit
        invoices = client.fetch_invoices(
            is_sales=is_sales,
            limit=fetch_limit,
            filters=",".join(filters) if filters else None,
        )

        if args.unsent:
            invoices = [inv for inv in invoices if not inv.ksef_status or inv.ksef_status == "N/A"]
            invoices = invoices[: args.limit]

        if not invoices:
            print("No invoices found.")
            return

        # Header definition based on verbosity
        if args.verbose:
            h1 = f"{'ID':<38} | {'Date':<10} | {'Inv No':<15} | {'Gross':<10} | "
            h2 = f"{'Paid':<10} | {'Tax No':<12} | {'Email':<25} | {'Payer'}"
            header = h1 + h2
        else:
            h1 = f"{'ID':<38} | {'Date':<10} | {'Inv No':<15} | {'Gross':<10} | "
            h2 = f"{'Paid Date':<10} | {'Payer':<30} | {'KSeF'}"
            header = h1 + h2

        print(header)
        print("-" * len(header))
        for inv in invoices:
            date = inv.issue_date or "N/A"
            gross = inv.gross_amount or "0.00"
            paid_date = inv.transfer_date or "N/A"

            if args.verbose:
                tax_no = inv.payer_tax_no or "N/A"
                email = (inv.payer_email or "N/A")[:25]
                payer = inv.payer_name or ""
                p1 = f"{inv.id:<38} | {date:<10} | {inv.invoice_no:<15} | {gross:<10} | "
                p2 = f"{paid_date:<10} | {tax_no:<12} | {email:<25} | {payer}"
                print(p1 + p2)
            else:
                payer = (inv.payer_name or "")[:30]
                ksef = inv.ksef_status or "N/A"
                p1 = f"{inv.id:<38} | {date:<10} | {inv.invoice_no:<15} | {gross:<10} | "
                p2 = f"{paid_date:<10} | {payer:<30} | {ksef}"
                print(p1 + p2)

    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def handle_invoices_mark_paid(args: argparse.Namespace) -> None:
    config = load_config()
    token = config.get("token")
    if not token:
        print("Not logged in. Run 'scanye login' first.", file=sys.stderr)
        sys.exit(1)

    client = ScanyeClient(token=token, debug=args.debug)
    try:
        client.mark_as_paid(args.invoice_ids, transfer_date=args.date)
        print(f"Successfully marked {len(args.invoice_ids)} invoices as paid.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def handle_invoices_send_ksef(args: argparse.Namespace) -> None:
    config = load_config()
    token = config.get("token")
    if not token:
        print("Not logged in. Run 'scanye login' first.", file=sys.stderr)
        sys.exit(1)

    client = ScanyeClient(token=token, debug=args.debug)
    invoice_ids = args.invoice_ids or []

    try:
        if args.all:
            print("Searching for unsent sales invoices...")
            # Fetch invoices from last few months to be safe
            now = datetime.now()
            start_month = f"{now.year if now.month > 1 else now.year - 1}-{max(1, (now.month - 2) % 12 or 12):02d}"
            filters = [
                "dateAuthenticated?isNotNull",
                f"annotations.accountingMonth>={start_month}",
            ]

            invoices = client.fetch_invoices(
                is_sales=True,
                limit=100,
                filters=",".join(filters),
            )

            # Filter for invoices that are not sent to KSeF
            to_send = [inv.id for inv in invoices if not inv.ksef_status or inv.ksef_status == "N/A"]

            if not to_send:
                print("No unsent invoices found.")
                return

            print(f"Found {len(to_send)} unsent invoices.")
            invoice_ids.extend(to_send)

        if not invoice_ids:
            print("No invoice IDs provided and --all not specified.", file=sys.stderr)
            sys.exit(1)

        print(f"Sending {len(invoice_ids)} invoices to KSeF...")
        client.send_to_ksef(invoice_ids)
        print("Successfully initiated sending to KSeF.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scanye CLI tool")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Login command
    login_parser = subparsers.add_parser("login", help="Login to Scanye")
    login_parser.add_argument("--email", required=True, help="Your Scanye email")

    # Invoices command
    invoice_parser = subparsers.add_parser("invoices", help="Invoice operations")
    invoice_subparsers = invoice_parser.add_subparsers(dest="subcommand", help="Invoice subcommands")

    list_parser = invoice_subparsers.add_parser("list", help="List invoices")
    list_parser.add_argument("--type", choices=["sales", "purchase"], default="sales", help="Invoice type")
    list_parser.add_argument("--limit", type=int, default=10, help="Limit number of invoices")
    list_parser.add_argument("--unsent", action="store_true", help="List only unsent to KSeF")
    list_parser.add_argument("--month", action="append", help="Month(s) to fetch (YYYY-MM), e.g. 2026-05")
    list_parser.add_argument("--filter", help="Raw filter string for API")
    list_parser.add_argument("-v", "--verbose", action="store_true", help="Show more details (NIP, email)")

    paid_parser = invoice_subparsers.add_parser("mark-paid", help="Mark invoices as paid")
    paid_parser.add_argument("invoice_ids", nargs="+", help="Invoice IDs to mark as paid")
    paid_parser.add_argument("--date", help="Transfer order date (YYYY-MM-DD), defaults to today")

    ksef_parser = invoice_subparsers.add_parser("send-ksef", help="Send invoices to KSeF")
    ksef_parser.add_argument("invoice_ids", nargs="*", help="Specific invoice IDs to send")
    ksef_parser.add_argument("--all", action="store_true", help="Automatically send all unsent sales invoices")

    args = parser.parse_args()

    if args.command == "login":
        handle_login(args)
    elif args.command == "invoices":
        if args.subcommand == "list":
            handle_invoices_list(args)
        elif args.subcommand == "mark-paid":
            handle_invoices_mark_paid(args)
        elif args.subcommand == "send-ksef":
            handle_invoices_send_ksef(args)
        else:
            parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
