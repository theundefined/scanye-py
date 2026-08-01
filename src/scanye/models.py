from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Invoice:
    id: str
    invoice_no: str
    is_sales: bool
    # The other party on the invoice: the buyer for sales invoices, the seller for purchase invoices.
    counterparty_name: Optional[str]
    counterparty_tax_no: Optional[str]
    counterparty_email: Optional[str]
    issue_date: Optional[str]
    due_date: Optional[str]
    transfer_date: Optional[str]
    gross_amount: Optional[str]
    date_authenticated: Optional[str]
    accounting_month: Optional[str]
    ksef_status: Optional[str]
    ksef_reference: Optional[str]
    origin: Optional[str]
    raw_data: Dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Invoice":
        inner_data = data.get("data", {})
        annotations = data.get("annotations", {})

        # New parsing logic for nested 'value' fields
        def get_val(obj: Any, key: str) -> Any:
            if isinstance(obj, dict):
                val_obj = obj.get(key)
                if isinstance(val_obj, dict) and "value" in val_obj:
                    return val_obj["value"]
                return val_obj
            return None

        # KSeF Status logic
        ksef_info = data.get("sentToKsef")
        ksef_status = None
        ksef_reference = None

        if isinstance(ksef_info, dict):
            ksef_status = ksef_info.get("type")
            ksef_reference = ksef_info.get("ksefReferenceNumber")

        # Check additional status fields (e.g. for ongoing sending)
        ksef_data = data.get("ksef", {})
        if isinstance(ksef_data, dict):
            if not ksef_status:
                ksef_status = ksef_data.get("sendingStatus") or ksef_data.get("status")
            if not ksef_reference:
                ksef_reference = ksef_data.get("referenceNumber")

        if not ksef_reference:
            ksef_reference = get_val(inner_data, "ksefNo")
            if ksef_reference and not ksef_status:
                ksef_status = "SENT"

        invoice_no = get_val(inner_data, "invoiceNo") or ""

        is_sales = (
            inner_data.get("accounting", {}).get("sales", {}).get("value") == "true"
            if isinstance(inner_data.get("accounting", {}).get("sales"), dict)
            else inner_data.get("accounting", {}).get("sales", False)
        )

        # "payer" is the buyer and "payee" is the seller; on a sales invoice the buyer is the
        # counterparty, on a purchase invoice the seller (payee) is.
        counterparty_obj = inner_data.get("payer", {}) if is_sales else inner_data.get("payee", {})
        counterparty_name = get_val(counterparty_obj, "name")
        counterparty_tax_no = get_val(counterparty_obj, "taxNo")

        # Only observed for sales invoices sent via Scanye's own KSeF issuance flow.
        counterparty_email = None
        issued_invoice = data.get("issuedInvoice", {})
        if isinstance(issued_invoice, dict):
            counterparty_email = issued_invoice.get("sentToBuyer", {}).get("email")
            if not counterparty_email:
                counterparty_email = issued_invoice.get("data", {}).get("buyer", {}).get("email")

        dates = inner_data.get("dates", {})
        issue_date = get_val(dates, "issue")
        due_date = get_val(dates, "due")

        amounts = inner_data.get("amounts", {})
        gross_amount = get_val(amounts, "gross")

        return cls(
            id=data.get("id", ""),
            invoice_no=invoice_no,
            is_sales=is_sales,
            counterparty_name=counterparty_name,
            counterparty_tax_no=counterparty_tax_no,
            counterparty_email=counterparty_email,
            issue_date=issue_date,
            due_date=due_date,
            transfer_date=data.get("dateTransferOrdered"),
            gross_amount=gross_amount,
            date_authenticated=data.get("dateAuthenticated"),
            accounting_month=annotations.get("accountingMonth"),
            ksef_status=ksef_status,
            ksef_reference=ksef_reference,
            origin=data.get("origin"),
            raw_data=data,
        )
