from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


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
    net_amount: Optional[str]
    vat_amount: Optional[str]
    currency: Optional[str]
    payment_method: Optional[str]
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
        net_amount = get_val(amounts, "net")
        vat_amount = get_val(amounts, "vat")

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
            net_amount=net_amount,
            vat_amount=vat_amount,
            currency=get_val(inner_data, "currency"),
            payment_method=get_val(inner_data, "paymentMethod"),
            date_authenticated=data.get("dateAuthenticated"),
            accounting_month=annotations.get("accountingMonth"),
            ksef_status=ksef_status,
            ksef_reference=ksef_reference,
            origin=data.get("origin"),
            raw_data=data,
        )

    def history(self) -> List[Tuple[str, str]]:
        """
        Returns (date, operation) entries derived from the raw payload's date* fields
        (dateCreated, dateSentToKsef, etc.), sorted chronologically. Mirrors the invoice
        history shown on the invoice detail page in the web app.
        """
        raw = self.raw_data
        issued_invoice = raw.get("issuedInvoice")
        issued_invoice = issued_invoice if isinstance(issued_invoice, dict) else {}

        entries: List[Tuple[str, str]] = []

        def add(date: Any, label: str) -> None:
            if date:
                entries.append((date, label))

        add(raw.get("dateCreated"), "Utworzono")
        add(raw.get("dateSentToAccounting"), "Wysłano do księgowości")
        add(raw.get("dateSucceeded"), "Przetworzono")
        add(raw.get("dateValidated"), "Zweryfikowano")
        add(raw.get("dateAuthenticated"), "Zautoryzowano")

        sent_to_ksef = issued_invoice.get("sentToKsef")
        sent_to_ksef = sent_to_ksef if isinstance(sent_to_ksef, dict) else {}
        ksef_reference = sent_to_ksef.get("ksefReferenceNumber") or self.ksef_reference
        ksef_label = f"Wysłano do KSeF (nr: {ksef_reference})" if ksef_reference else "Wysłano do KSeF"
        add(sent_to_ksef.get("dateSend"), ksef_label)

        export_target = raw.get("exportTarget")
        export_label = f"Wyeksportowano ({export_target})" if export_target else "Wyeksportowano"
        add(raw.get("dateExported"), export_label)

        sent_to_buyer = issued_invoice.get("sentToBuyer")
        sent_to_buyer = sent_to_buyer if isinstance(sent_to_buyer, dict) else {}
        buyer_email = sent_to_buyer.get("email")
        email_label = f"Wysłano do nabywcy e-mailem ({buyer_email})" if buyer_email else "Wysłano do nabywcy e-mailem"
        add(raw.get("dateSentToBuyer"), email_label)

        add(self.transfer_date, "Oznaczono jako zapłacona")

        entries.sort(key=lambda entry: entry[0])
        return entries
