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

import click

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


def handle_login(email: str, debug: bool) -> None:
    password = getpass(f"Password for {email}: ")

    client = ScanyeClient(debug=debug)
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


def _trim_to_full_days(invoices: List[Invoice], limit: int) -> List[Invoice]:
    """
    Returns at most `limit` invoices (already sorted newest first), but never splits the
    invoices issued on one day between kept and dropped. The server has no defined tie-break
    for same-day invoices, so cutting off mid-day would show an arbitrary subset of that day
    rather than a meaningful "most recent N" -- better to show fewer, ending on a full day.
    """
    if len(invoices) <= limit:
        return invoices
    boundary_date = invoices[limit - 1].issue_date
    if invoices[limit].issue_date != boundary_date:
        return invoices[:limit]
    trimmed = [inv for inv in invoices[:limit] if inv.issue_date != boundary_date]
    return trimmed or invoices[:limit]


def handle_invoices_list(
    invoice_type: str,
    limit: Optional[int],
    unsent: bool,
    month: Optional[List[str]],
    raw_filter: Optional[str],
    verbose: bool,
    debug: bool,
) -> None:
    config = load_config()
    require_credentials(config)

    is_sales = invoice_type == "sales"
    client = build_client(config, debug)
    # Default to the current accounting month rather than a raw invoice count, so the
    # displayed set has a meaningful boundary instead of an arbitrary server-side cutoff.
    months = month or [datetime.now().strftime("%Y-%m")]
    filters = build_month_filters(months, raw_filter)

    display_limit = limit or (10 if unsent else None)

    try:
        if display_limit:
            fetch_limit = display_limit * 5 if unsent else display_limit + 20
        else:
            fetch_limit = 1000

        invoices = client.fetch_invoices(
            is_sales=is_sales,
            limit=fetch_limit,
            filters=",".join(filters) if filters else None,
        )

        invoices.sort(key=_invoice_sort_key, reverse=True)

        if unsent:
            invoices = [inv for inv in invoices if not inv.ksef_status or inv.ksef_status == "N/A"]

        if display_limit:
            invoices = _trim_to_full_days(invoices, display_limit)

        if not invoices:
            print("No invoices found.")
            return

        # "Client" for sales invoices (the buyer), "Seller" for purchase invoices (the vendor).
        counterparty_label = "Client" if is_sales else "Seller"

        # Header definition based on verbosity
        if verbose:
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

            if verbose:
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


def _print_invoice_details(inv: Invoice) -> None:
    print(f"{inv.invoice_no}  ({'sales' if inv.is_sales else 'purchase'})")
    print(f"ID: {inv.id}")
    counterparty_label = "Client" if inv.is_sales else "Seller"
    print(f"{counterparty_label}: {inv.counterparty_name or 'N/A'} (Tax No: {inv.counterparty_tax_no or 'N/A'})")
    if inv.counterparty_email:
        print(f"Email: {inv.counterparty_email}")
    print(f"Issue date: {inv.issue_date or 'N/A'}    Due date: {inv.due_date or 'N/A'}")
    currency = inv.currency or ""
    print(
        f"Amount: {inv.gross_amount or 'N/A'} {currency} gross "
        f"({inv.net_amount or 'N/A'} net, {inv.vat_amount or 'N/A'} VAT)"
    )
    if inv.payment_method:
        print(f"Payment method: {inv.payment_method}")
    print(f"KSeF: {inv.ksef_status or 'N/A'}" + (f" ({inv.ksef_reference})" if inv.ksef_reference else ""))
    print(f"Paid: {inv.transfer_date or 'Not paid'}")
    if inv.accounting_month:
        print(f"Accounting month: {inv.accounting_month}")

    print("\nHistory:")
    history = inv.history()
    if not history:
        print("  No history available.")
        return
    for date, operation in history:
        print(f"  {date:<26} | {operation}")


def handle_invoices_show(invoice_id: str, debug: bool) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, debug)
    try:
        invoice = client.get_invoice(invoice_id)
        if not invoice:
            print(f"Invoice {invoice_id} not found.", file=sys.stderr)
            sys.exit(1)
        _print_invoice_details(invoice)
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def handle_invoices_mark_paid(invoice_ids: List[str], date: Optional[str], debug: bool) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, debug)
    try:
        client.mark_as_paid(invoice_ids, transfer_date=date)
        print(f"Successfully marked {len(invoice_ids)} invoices as paid.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def handle_invoices_mark_unpaid(invoice_ids: List[str], debug: bool) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, debug)
    try:
        client.mark_as_unpaid(invoice_ids)
        print(f"Successfully marked {len(invoice_ids)} invoices as unpaid.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def handle_invoices_send_ksef(invoice_ids: List[str], send_all: bool, debug: bool) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, debug)
    invoice_ids = invoice_ids or []

    try:
        if send_all:
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


def handle_invoices_send_email(invoice_id: str, to: str, no_save_email: bool, debug: bool) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, debug)
    try:
        client.send_to_buyer(invoice_id, to, save_email=not no_save_email)
        print(f"Successfully sent invoice {invoice_id} to {to}.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def handle_invoices_download(
    invoice_ids: List[str],
    invoice_type: str,
    month: Optional[List[str]],
    raw_filter: Optional[str],
    limit: int,
    output: str,
    debug: bool,
) -> None:
    config = load_config()
    require_credentials(config)

    if invoice_ids and (month or raw_filter):
        print("Cannot combine specific invoice IDs with --month/--filter.", file=sys.stderr)
        sys.exit(1)

    is_sales = invoice_type == "sales"
    client = build_client(config, debug)

    try:
        if not invoice_ids:
            filters = build_month_filters(month, raw_filter)
            invoices = client.fetch_invoices(
                is_sales=is_sales,
                limit=limit,
                filters=",".join(filters),
            )
            invoice_ids = [inv.id for inv in invoices]

        if not invoice_ids:
            print("No invoices found to download.")
            return

        output_dir = Path(output)
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


@click.group(invoke_without_command=True)
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, debug: bool) -> None:
    """Scanye CLI tool"""
    ctx.obj = debug
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.option("--email", required=True, help="Your Scanye email")
@click.pass_obj
def login(debug: bool, email: str) -> None:
    """Login to Scanye"""
    handle_login(email, debug)


@cli.group(invoke_without_command=True)
@click.pass_context
def invoices(ctx: click.Context) -> None:
    """Invoice operations"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@invoices.command(name="list")
@click.option("--type", "invoice_type", type=click.Choice(["sales", "purchase"]), default="sales", help="Invoice type")
@click.option("--limit", type=int, default=None, help="Max invoices to show (default: no cap; whole month is shown)")
@click.option("--unsent", is_flag=True, help="List only unsent to KSeF")
@click.option("--month", multiple=True, help="Month(s) to fetch (YYYY-MM), e.g. 2026-05. Default: current month")
@click.option("--filter", "raw_filter", help="Raw filter string for API")
@click.option("-v", "--verbose", is_flag=True, help="Show more details (NIP, email)")
@click.pass_obj
def invoices_list(
    debug: bool,
    invoice_type: str,
    limit: Optional[int],
    unsent: bool,
    month: tuple,
    raw_filter: Optional[str],
    verbose: bool,
) -> None:
    """List invoices"""
    handle_invoices_list(
        invoice_type=invoice_type,
        limit=limit,
        unsent=unsent,
        month=list(month) if month else None,
        raw_filter=raw_filter,
        verbose=verbose,
        debug=debug,
    )


@invoices.command(name="show")
@click.argument("invoice_id")
@click.pass_obj
def invoices_show(debug: bool, invoice_id: str) -> None:
    """Show invoice details and history"""
    handle_invoices_show(invoice_id, debug)


@invoices.command(name="mark-paid")
@click.argument("invoice_ids", nargs=-1, required=True)
@click.option("--date", help="Transfer order date (YYYY-MM-DD), defaults to today")
@click.pass_obj
def invoices_mark_paid(debug: bool, invoice_ids: tuple, date: Optional[str]) -> None:
    """Mark invoices as paid"""
    handle_invoices_mark_paid(list(invoice_ids), date, debug)


@invoices.command(name="mark-unpaid")
@click.argument("invoice_ids", nargs=-1, required=True)
@click.pass_obj
def invoices_mark_unpaid(debug: bool, invoice_ids: tuple) -> None:
    """Mark invoices as unpaid"""
    handle_invoices_mark_unpaid(list(invoice_ids), debug)


@invoices.command(name="send-ksef")
@click.argument("invoice_ids", nargs=-1)
@click.option("--all", "send_all", is_flag=True, help="Automatically send all unsent sales invoices")
@click.pass_obj
def invoices_send_ksef(debug: bool, invoice_ids: tuple, send_all: bool) -> None:
    """Send invoices to KSeF"""
    handle_invoices_send_ksef(list(invoice_ids), send_all, debug)


@invoices.command(name="send-email")
@click.argument("invoice_id")
@click.option("--to", required=True, help="Recipient e-mail address")
@click.option("--no-save-email", is_flag=True, help="Don't remember this address for next time")
@click.pass_obj
def invoices_send_email(debug: bool, invoice_id: str, to: str, no_save_email: bool) -> None:
    """Send an invoice to its buyer by e-mail"""
    handle_invoices_send_email(invoice_id, to, no_save_email, debug)


@invoices.command(name="download")
@click.argument("invoice_ids", nargs=-1)
@click.option("--type", "invoice_type", type=click.Choice(["sales", "purchase"]), default="sales", help="Invoice type")
@click.option("--month", multiple=True, help="Month(s) to fetch (YYYY-MM), e.g. 2026-07")
@click.option("--filter", "raw_filter", help="Raw filter string for API")
@click.option("--limit", type=int, default=100, help="Max invoices to download when using filters")
@click.option("-o", "--output", default=".", help="Output directory (default: current directory)")
@click.pass_obj
def invoices_download(
    debug: bool,
    invoice_ids: tuple,
    invoice_type: str,
    month: tuple,
    raw_filter: Optional[str],
    limit: int,
    output: str,
) -> None:
    """Download invoices as PDF"""
    handle_invoices_download(
        invoice_ids=list(invoice_ids),
        invoice_type=invoice_type,
        month=list(month) if month else None,
        raw_filter=raw_filter,
        limit=limit,
        output=output,
        debug=debug,
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
