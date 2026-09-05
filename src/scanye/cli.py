import argparse
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from getpass import getpass
from pathlib import Path
from typing import List, Optional

from .client import ScanyeClient
from .exceptions import ScanyeError
from .models import Invoice

CONFIG_DIR = Path.home() / ".config" / "scanye"
CONFIG_FILE = CONFIG_DIR / "config.json"


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    os.chmod(CONFIG_FILE, 0o600)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        if isinstance(config, dict):
            return config
        return {}


def build_client(config: dict, debug: bool) -> ScanyeClient:
    return ScanyeClient(
        token=config.get("token"),
        debug=debug,
        email=config.get("email"),
        password=config.get("password"),
    )


def persist_token(config: dict, client: ScanyeClient) -> None:
    """Save the client's current token if it changed (e.g. after an automatic re-login)."""
    if client.token and client.token != config.get("token"):
        config["token"] = client.token
        save_config(config)


def require_credentials(config: dict) -> None:
    if not config.get("token") and not (config.get("email") and config.get("password")):
        print("Not logged in. Run 'scanye login' first.", file=sys.stderr)
        sys.exit(1)


def handle_login(args: argparse.Namespace) -> None:
    email = args.email
    password = getpass(f"Password for {email}: ")

    client = ScanyeClient(debug=args.debug)
    try:
        token = client.login(email, password)
        config = {"token": token, "email": email}

        answer = input("Save password so expired tokens can be refreshed automatically? [y/N]: ")
        if answer.strip().lower() in ("y", "yes"):
            config["password"] = password
            save_config(config)
            print("Login successful. Token and password saved.")
        else:
            save_config(config)
            print("Login successful. Token saved.")
    except ScanyeError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)


def build_month_filters(months: Optional[List[str]], raw_filter: Optional[str]) -> List[str]:
    filters = ["dateAuthenticated?isNotNull"]
    # Note: "unsent" isn't a real server-side field; callers apply it client-side
    # after fetching, once ksef_status has been resolved from the raw invoice payload.

    if raw_filter:
        filters.append(raw_filter)
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

    return filters


def _invoice_sort_key(inv: Invoice) -> tuple:
    try:
        date_key = datetime.strptime(inv.issue_date or "", "%d.%m.%Y")
    except ValueError:
        date_key = datetime.min
    # Invoice numbers look like "FV/26/09/3"; sort by the trailing sequence number
    # numerically so "10" sorts after "9" instead of before it lexicographically.
    match = re.search(r"(\d+)$", inv.invoice_no or "")
    number_key = int(match.group(1)) if match else 0
    return (date_key, number_key)


def handle_invoices_list(args: argparse.Namespace) -> None:
    config = load_config()
    require_credentials(config)

    is_sales = args.type == "sales"
    client = build_client(config, args.debug)
    filters = build_month_filters(args.month, args.filter)

    try:
        # Fetch more if we are filtering client-side
        fetch_limit = args.limit * 5 if args.unsent else args.limit
        invoices = client.fetch_invoices(
            is_sales=is_sales,
            limit=fetch_limit,
            filters=",".join(filters) if filters else None,
        )

        invoices.sort(key=_invoice_sort_key, reverse=True)

        if args.unsent:
            invoices = [inv for inv in invoices if not inv.ksef_status or inv.ksef_status == "N/A"]
            invoices = invoices[: args.limit]

        if not invoices:
            print("No invoices found.")
            return

        # "Client" for sales invoices (the buyer), "Seller" for purchase invoices (the vendor).
        counterparty_label = "Client" if is_sales else "Seller"

        # Header definition based on verbosity
        if args.verbose:
            h1 = f"{'ID':<38} | {'Date':<10} | {'Inv No':<15} | {'Gross':<10} | "
            h2 = f"{'Paid':<10} | {'Tax No':<12} | {'Email':<25} | {counterparty_label}"
            header = h1 + h2
        else:
            h1 = f"{'ID':<38} | {'Date':<10} | {'Inv No':<15} | {'Gross':<10} | "
            h2 = f"{'Paid Date':<10} | {counterparty_label:<30} | {'KSeF'}"
            header = h1 + h2

        print(header)
        print("-" * len(header))
        for inv in invoices:
            date = inv.issue_date or "N/A"
            gross = inv.gross_amount or "0.00"
            paid_date = inv.transfer_date or "N/A"

            if args.verbose:
                tax_no = inv.counterparty_tax_no or "N/A"
                email = (inv.counterparty_email or "N/A")[:25]
                counterparty = inv.counterparty_name or ""
                p1 = f"{inv.id:<38} | {date:<10} | {inv.invoice_no:<15} | {gross:<10} | "
                p2 = f"{paid_date:<10} | {tax_no:<12} | {email:<25} | {counterparty}"
                print(p1 + p2)
            else:
                counterparty = (inv.counterparty_name or "")[:30]
                ksef = inv.ksef_status or "N/A"
                p1 = f"{inv.id:<38} | {date:<10} | {inv.invoice_no:<15} | {gross:<10} | "
                p2 = f"{paid_date:<10} | {counterparty:<30} | {ksef}"
                print(p1 + p2)

    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def handle_invoices_mark_paid(args: argparse.Namespace) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, args.debug)
    try:
        client.mark_as_paid(args.invoice_ids, transfer_date=args.date)
        print(f"Successfully marked {len(args.invoice_ids)} invoices as paid.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def handle_invoices_mark_unpaid(args: argparse.Namespace) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, args.debug)
    try:
        client.mark_as_unpaid(args.invoice_ids)
        print(f"Successfully marked {len(args.invoice_ids)} invoices as unpaid.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def handle_invoices_send_ksef(args: argparse.Namespace) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, args.debug)
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
    finally:
        persist_token(config, client)


def handle_invoices_download(args: argparse.Namespace) -> None:
    config = load_config()
    require_credentials(config)

    if args.invoice_ids and (args.month or args.filter):
        print("Cannot combine specific invoice IDs with --month/--filter.", file=sys.stderr)
        sys.exit(1)

    is_sales = args.type == "sales"
    client = build_client(config, args.debug)

    try:
        if args.invoice_ids:
            invoice_ids = args.invoice_ids
        else:
            filters = build_month_filters(args.month, args.filter)
            invoices = client.fetch_invoices(
                is_sales=is_sales,
                limit=args.limit,
                filters=",".join(filters),
            )
            invoice_ids = [inv.id for inv in invoices]

        if not invoice_ids:
            print("No invoices found to download.")
            return

        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)

        print(f"Downloading {len(invoice_ids)} invoice(s)...")
        content, filename = client.fetch_printout(invoice_ids)

        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
                zf.extractall(output_dir)
            print(f"Saved {len(names)} file(s) to {output_dir}/")
        else:
            path = output_dir / Path(filename).name
            path.write_bytes(content)
            print(f"Saved {path}")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


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

    unpaid_parser = invoice_subparsers.add_parser("mark-unpaid", help="Mark invoices as unpaid")
    unpaid_parser.add_argument("invoice_ids", nargs="+", help="Invoice IDs to mark as unpaid")

    ksef_parser = invoice_subparsers.add_parser("send-ksef", help="Send invoices to KSeF")
    ksef_parser.add_argument("invoice_ids", nargs="*", help="Specific invoice IDs to send")
    ksef_parser.add_argument("--all", action="store_true", help="Automatically send all unsent sales invoices")

    download_parser = invoice_subparsers.add_parser("download", help="Download invoices as PDF")
    download_parser.add_argument(
        "invoice_ids", nargs="*", help="Specific invoice IDs to download (omit to use --month/--filter instead)"
    )
    download_parser.add_argument("--type", choices=["sales", "purchase"], default="sales", help="Invoice type")
    download_parser.add_argument("--month", action="append", help="Month(s) to fetch (YYYY-MM), e.g. 2026-07")
    download_parser.add_argument("--filter", help="Raw filter string for API")
    download_parser.add_argument("--limit", type=int, default=100, help="Max invoices to download when using filters")
    download_parser.add_argument("-o", "--output", default=".", help="Output directory (default: current directory)")

    args = parser.parse_args()

    if args.command == "login":
        handle_login(args)
    elif args.command == "invoices":
        if args.subcommand == "list":
            handle_invoices_list(args)
        elif args.subcommand == "mark-paid":
            handle_invoices_mark_paid(args)
        elif args.subcommand == "mark-unpaid":
            handle_invoices_mark_unpaid(args)
        elif args.subcommand == "send-ksef":
            handle_invoices_send_ksef(args)
        elif args.subcommand == "download":
            handle_invoices_download(args)
        else:
            invoice_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
