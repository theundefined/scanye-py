import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import click

from .client import ScanyeClient, iter_document_fields
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


def _pending_invoice_ids(binders: List[dict]) -> tuple:
    """
    Returns (pending_ids, skipped_non_invoice_count) from a fetch_binders() result: artifacts
    that are invoices, not yet authenticated (confirmed), and not deleted.
    """
    pending_ids: List[str] = []
    skipped_non_invoice = 0
    for binder in binders:
        if binder.get("dateDeleted"):
            continue
        for artifact in binder.get("artifacts", []):
            if artifact.get("dateAuthenticated") or artifact.get("dateDeleted"):
                continue
            if artifact.get("artifactType") != "Invoice":
                skipped_non_invoice += 1
                continue
            pending_ids.append(artifact["id"])
    return pending_ids, skipped_non_invoice


def _print_pending_invoices(client: ScanyeClient, pending_ids: List[str], invoice_type: str) -> None:
    header = f"{'ID':<38} | {'Date':<10} | {'Type':<8} | {'Inv No':<15} | {'Gross':<10} | {'Seller/Client'}"
    rows = []
    for invoice_id in pending_ids:
        invoice = client.get_invoice(invoice_id)
        if invoice is None:
            continue
        if invoice_type != "all" and invoice.is_sales != (invoice_type == "sales"):
            continue
        rows.append(invoice)

    if not rows:
        print("No matching invoices.")
        return

    print(header)
    print("-" * len(header))
    for inv in rows:
        direction = "sales" if inv.is_sales else "purchase"
        currency = f" {inv.currency}" if inv.currency else ""
        gross = f"{inv.gross_amount or 'N/A'}{currency}"
        counterparty = (inv.counterparty_name or "")[:40]
        p1 = f"{inv.id:<38} | {inv.issue_date or 'N/A':<10} | {direction:<8} | "
        p2 = f"{inv.invoice_no or 'N/A':<15} | {gross:<10} | {counterparty}"
        print(p1 + p2)


def _open_file(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def _view_invoice_document(client: ScanyeClient, invoice: Invoice) -> None:
    num_pages = invoice.raw_data.get("numPages") or 1
    tmp_dir = Path(tempfile.gettempdir()) / "scanye-invoice-pages"
    tmp_dir.mkdir(exist_ok=True)

    try:
        for page in range(1, num_pages + 1):
            content, content_type = client.download_invoice_page(invoice.id, page)
            ext = mimetypes.guess_extension(content_type) or ".bin"
            path = tmp_dir / f"{invoice.id}-p{page}{ext}"
            path.write_bytes(content)
            print(f"  Saved page {page} to {path} (opening...)")
            _open_file(path)
    except ScanyeError as e:
        print(f"  Could not load the document: {e}")


def _format_field_path(path: Tuple[Any, ...]) -> str:
    parts: List[str] = []
    for part in path:
        if isinstance(part, int):
            parts[-1] += f"[{part + 1}]"
        else:
            parts.append(str(part))
    return ".".join(parts)


_AMOUNT_KEYS = ("gross", "net", "vat", "due")


def _amount_group_key(path: Tuple[Any, ...]) -> Optional[Tuple[Any, ...]]:
    """
    Path's parent if this leaf belongs to an amounts-like group -- the invoice-level `amounts`
    object, or a single entry of the `amountsPerRate` list -- else None.
    """
    if len(path) >= 2 and path[-1] in _AMOUNT_KEYS:
        parent = path[:-1]
        if parent == ("amounts",) or (len(parent) == 2 and parent[0] == "amountsPerRate"):
            return parent
    return None


def _reorder_amount_groups(fields: List[Tuple[Tuple[Any, ...], Any]]) -> List[Tuple[Tuple[Any, ...], Any]]:
    """
    Moves each amounts-like group's fields together, in gross/net/vat/due order, at the position
    of the group's first field -- gross and net are what's actually worth typing, so they must
    come before vat/due (and, for a single-rate invoice, before amountsPerRate) for those to be
    auto-suggested from them.
    """
    groups: Dict[Tuple[Any, ...], Dict[str, Tuple[Tuple[Any, ...], Any]]] = {}
    for path, value in fields:
        group = _amount_group_key(path)
        if group is not None:
            groups.setdefault(group, {})[path[-1]] = (path, value)

    emitted: Set[Tuple[Any, ...]] = set()
    result: List[Tuple[Tuple[Any, ...], Any]] = []
    for path, value in fields:
        group = _amount_group_key(path)
        if group is None:
            result.append((path, value))
            continue
        if group in emitted:
            continue
        emitted.add(group)
        for key in _AMOUNT_KEYS:
            if key in groups[group]:
                result.append(groups[group][key])
    return result


def _compute_derived_amount(key: str, context: Dict[str, str]) -> Optional[str]:
    try:
        if key == "vat" and "gross" in context and "net" in context:
            gross = Decimal(context["gross"])
            net = Decimal(context["net"])
            return str((gross - net).quantize(Decimal("0.01")))
        if key == "due" and "gross" in context:
            return context["gross"]
    except InvalidOperation:
        return None
    return None


def _edit_invoice_fields(data: Dict[str, Any], is_sales: bool, overrides: Dict[Tuple[Any, ...], str]) -> None:
    print("  Reviewing every field -- Enter keeps the current value, 'q' stops early.")
    print("  Amounts: enter gross/net -- vat/due (and a single VAT rate's amounts) get suggested from them.")
    fields = _reorder_amount_groups(list(iter_document_fields(data, is_sales)))
    per_rate_groups = {
        group for path, _ in fields if (group := _amount_group_key(path)) is not None and group[0] == "amountsPerRate"
    }
    single_rate = len(per_rate_groups) == 1

    context: Dict[Tuple[Any, ...], Dict[str, str]] = {}
    for path, current_value in fields:
        group = _amount_group_key(path)
        key = path[-1] if group is not None else None
        shown = overrides.get(path, current_value)

        if group is not None and key is not None and path not in overrides:
            top = context.get(("amounts",), {})
            if single_rate and group != ("amounts",) and key in top:
                shown = top[key]
            else:
                computed = _compute_derived_amount(key, context.get(group, {}))
                if computed is not None:
                    shown = computed

        answer = input(f"    {_format_field_path(path)} [{shown}]: ").strip()
        if answer.lower() == "q":
            return

        effective = answer if answer else str(shown)
        if group is not None and key is not None:
            context.setdefault(group, {})[key] = effective
        if answer:
            overrides[path] = answer
        elif group is not None and effective != str(current_value):
            overrides[path] = effective


def handle_invoices_confirm(
    month: Optional[str], invoice_type: str, dry_run: bool, list_only: bool, debug: bool
) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, debug)
    month = month or datetime.now().strftime("%Y-%m")

    try:
        binders = client.fetch_binders(month)
        pending_ids, skipped_non_invoice = _pending_invoice_ids(binders)

        if not pending_ids:
            print(f"No invoices pending confirmation for {month}.")
            if skipped_non_invoice:
                print(f"({skipped_non_invoice} non-invoice document(s) in the inbox were skipped.)")
            return

        if list_only:
            _print_pending_invoices(client, pending_ids, invoice_type)
            if skipped_non_invoice:
                print(f"\n({skipped_non_invoice} non-invoice document(s) in the inbox were skipped.)")
            return

        print(f"{len(pending_ids)} invoice(s) pending confirmation for {month}.")
        if skipped_non_invoice:
            print(f"({skipped_non_invoice} non-invoice document(s) in the inbox were skipped.)")

        confirmed = 0
        skipped = 0
        for invoice_id in pending_ids:
            invoice = client.get_invoice(invoice_id)
            if invoice is None:
                print(f"\n{invoice_id}: not found, skipping.")
                skipped += 1
                continue

            if invoice_type != "all" and invoice.is_sales != (invoice_type == "sales"):
                skipped += 1
                continue

            direction = "sales" if invoice.is_sales else "purchase"
            counterparty_label = "Client" if invoice.is_sales else "Seller"
            counterparty_key = "payer" if invoice.is_sales else "payee"

            overrides: Dict[Tuple[Any, ...], str] = {}
            document_data: Optional[Dict[str, Any]] = None

            while True:
                invoice_no = overrides.get(("invoiceNo",), invoice.invoice_no or "N/A")
                date = overrides.get(("dates", "issue"), invoice.issue_date or "N/A")
                gross = overrides.get(("amounts", "gross"), invoice.gross_amount or "N/A")
                currency = overrides.get(("currency",), invoice.currency or "")
                name = overrides.get((counterparty_key, "name"), invoice.counterparty_name or "N/A")
                tax_no = overrides.get((counterparty_key, "taxNo"), invoice.counterparty_tax_no or "N/A")

                print(f"\n{invoice_no} ({direction})    ID: {invoice.id}")
                print(f"  Date: {date}    Amount: {gross} {currency}".rstrip())
                print(f"  {counterparty_label}: {name} (Tax No: {tax_no})")
                if overrides:
                    print(f"  ({len(overrides)} field(s) edited)")

                prompt = "  [Enter] confirm  /  s = skip  /  v = view document  /  e = edit fields  /  q = quit: "
                answer = input(prompt).strip().lower()

                if answer == "v":
                    _view_invoice_document(client, invoice)
                    continue
                if answer == "e":
                    if document_data is None:
                        document_data = client.get_invoice_data(invoice_id)
                    _edit_invoice_fields(document_data, invoice.is_sales, overrides)
                    continue
                break

            if answer == "q":
                break
            if answer == "s":
                skipped += 1
                continue

            if dry_run:
                print("  (dry run, not confirmed)")
            else:
                client.confirm_invoice(invoice_id, field_overrides=overrides or None)
                print("  Confirmed.")
                confirmed += 1

        suffix = " (dry run)" if dry_run else ""
        print(f"\n{confirmed} confirmed{suffix}, {skipped} skipped.")
    except ScanyeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        persist_token(config, client)


def _prompt_transfer_date(client: ScanyeClient, invoice_ids: List[str]) -> Optional[str]:
    """
    Shows the relevant date fields for each invoice and asks for the payment (transfer order)
    date to apply to all of them. Returns None if the user cancels.
    """
    for invoice_id in invoice_ids:
        invoice = client.get_invoice(invoice_id)
        if invoice is None:
            print(f"{invoice_id}: not found")
            continue

        direction = "sales" if invoice.is_sales else "purchase"
        counterparty_label = "Client" if invoice.is_sales else "Seller"
        print(f"\n{invoice.invoice_no or 'N/A'} ({direction})    ID: {invoice.id}")
        name = invoice.counterparty_name or "N/A"
        tax_no = invoice.counterparty_tax_no or "N/A"
        print(f"  {counterparty_label}: {name} (Tax No: {tax_no})")
        print(f"  Issue date: {invoice.issue_date or 'N/A'}    Due date: {invoice.due_date or 'N/A'}")
        print(f"  Amount: {invoice.gross_amount or 'N/A'} {invoice.currency or ''}".rstrip())
        print(f"  Payment method: {invoice.payment_method or 'N/A'}")
        if invoice.transfer_date:
            print(f"  Already marked as paid on: {invoice.transfer_date}")

    today = datetime.now().strftime("%Y-%m-%d")
    while True:
        answer = input(f"\nPayment date (YYYY-MM-DD, Enter = {today}, q = cancel): ").strip()
        if answer.lower() == "q":
            return None
        if not answer:
            return today
        try:
            datetime.strptime(answer, "%Y-%m-%d")
            return answer
        except ValueError:
            print("Invalid date format, expected YYYY-MM-DD.")


def handle_invoices_mark_paid(invoice_ids: List[str], date: Optional[str], auto_today: bool, debug: bool) -> None:
    config = load_config()
    require_credentials(config)

    client = build_client(config, debug)
    try:
        if date:
            transfer_date: Optional[str] = date
        elif auto_today:
            transfer_date = datetime.now().strftime("%Y-%m-%d")
        else:
            transfer_date = _prompt_transfer_date(client, invoice_ids)
            if transfer_date is None:
                print("Cancelled.")
                return

        client.mark_as_paid(invoice_ids, transfer_date=transfer_date)
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


@invoices.command(name="confirm")
@click.option("--month", default=None, help="Accounting month to review (YYYY-MM). Default: current month")
@click.option(
    "--type",
    "invoice_type",
    type=click.Choice(["sales", "purchase", "all"]),
    default="all",
    help="Only review invoices of this type",
)
@click.option("--dry-run", is_flag=True, help="Show what would be confirmed without submitting anything")
@click.option(
    "--list", "list_only", is_flag=True, help="Just list invoices pending confirmation, without confirming any"
)
@click.pass_obj
def invoices_confirm(debug: bool, month: Optional[str], invoice_type: str, dry_run: bool, list_only: bool) -> None:
    """Step through invoices pending confirmation (inbox) one by one"""
    handle_invoices_confirm(month=month, invoice_type=invoice_type, dry_run=dry_run, list_only=list_only, debug=debug)


@invoices.command(name="mark-paid")
@click.argument("invoice_ids", nargs=-1, required=True)
@click.option("--date", help="Transfer order date (YYYY-MM-DD); skips the interactive prompt")
@click.option("--auto-today", is_flag=True, help="Use today's date without prompting, instead of asking interactively")
@click.pass_obj
def invoices_mark_paid(debug: bool, invoice_ids: tuple, date: Optional[str], auto_today: bool) -> None:
    """Mark invoices as paid"""
    handle_invoices_mark_paid(list(invoice_ids), date, auto_today, debug)


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
