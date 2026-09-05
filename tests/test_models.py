from scanye.models import Invoice


def test_invoice_from_dict():
    data = {
        "id": "123",
        "dateAuthenticated": "2026-05-08T10:00:00Z",
        "origin": "UPLOAD",
        "data": {
            "invoiceNo": {"value": "FV/2026/01"},
            "accounting": {"sales": {"value": "true"}},
            "payer": {"name": {"value": "Test Payer"}},
            "dates": {"issue": {"value": "2026-05-01"}, "due": {"value": "2026-05-15"}},
            "amounts": {"gross": {"value": "123.45"}},
            "ksefNo": {"value": "KSEF-REF-123"},
        },
        "annotations": {"accountingMonth": "2026-05"},
    }

    invoice = Invoice.from_dict(data)

    assert invoice.id == "123"
    assert invoice.invoice_no == "FV/2026/01"
    assert invoice.is_sales is True
    assert invoice.counterparty_name == "Test Payer"
    assert invoice.issue_date == "2026-05-01"
    assert invoice.gross_amount == "123.45"
    assert invoice.ksef_status == "SENT"
    assert invoice.ksef_reference == "KSEF-REF-123"


def test_invoice_from_dict_ksef_info():
    data = {
        "id": "456",
        "data": {
            "invoiceNo": "INV-456",
        },
        "sentToKsef": {"type": "PENDING", "ksefReferenceNumber": "REF-PENDING"},
    }

    invoice = Invoice.from_dict(data)

    assert invoice.id == "456"
    assert invoice.ksef_status == "PENDING"
    assert invoice.ksef_reference == "REF-PENDING"


def test_invoice_from_dict_ksef_sending_status():
    data = {
        "id": "789",
        "data": {
            "invoiceNo": "INV-789",
        },
        "ksef": {"sendingStatus": "QUEUED", "referenceNumber": "REF-QUEUED"},
    }

    invoice = Invoice.from_dict(data)

    assert invoice.id == "789"
    assert invoice.ksef_status == "QUEUED"
    assert invoice.ksef_reference == "REF-QUEUED"


def test_invoice_from_dict_purchase_shows_seller_as_counterparty():
    data = {
        "id": "999",
        "data": {
            "invoiceNo": {"value": "FV/2026/02"},
            "accounting": {"sales": {"value": "false"}},
            "payer": {"name": {"value": "My Own Company"}, "taxNo": {"value": "1111111111"}},
            "payee": {"name": {"value": "Some Vendor"}, "taxNo": {"value": "2222222222"}},
        },
    }

    invoice = Invoice.from_dict(data)

    assert invoice.is_sales is False
    assert invoice.counterparty_name == "Some Vendor"
    assert invoice.counterparty_tax_no == "2222222222"


def test_invoice_history_sorted_chronologically_with_labels():
    data = {
        "id": "123",
        "dateCreated": "2026-07-31T19:15:41.906358",
        "dateSentToAccounting": "2026-07-31T19:15:42.022917",
        "dateExported": "2026-08-14T17:17:34.376362",
        "dateSentToBuyer": "2026-08-31T17:36:00.285586",
        "dateTransferOrdered": "2026-09-01",
        "exportTarget": "RachmistrzNexo",
        "issuedInvoice": {
            "sentToKsef": {"dateSend": "2026-07-31T19:15:57.38307", "ksefReferenceNumber": "REF-1"},
            "sentToBuyer": {"date": "2026-08-31T17:36:00.285586", "email": "buyer@example.com"},
        },
        "data": {
            "invoiceNo": {"value": "FV/2026/01"},
            "accounting": {"sales": {"value": "true"}},
        },
    }

    invoice = Invoice.from_dict(data)
    history = invoice.history()

    assert history == [
        ("2026-07-31T19:15:41.906358", "Utworzono"),
        ("2026-07-31T19:15:42.022917", "Wysłano do księgowości"),
        ("2026-07-31T19:15:57.38307", "Wysłano do KSeF (nr: REF-1)"),
        ("2026-08-14T17:17:34.376362", "Wyeksportowano (RachmistrzNexo)"),
        ("2026-08-31T17:36:00.285586", "Wysłano do nabywcy e-mailem (buyer@example.com)"),
        ("2026-09-01", "Oznaczono jako zapłacona"),
    ]


def test_invoice_history_skips_missing_dates():
    data = {
        "id": "123",
        "dateCreated": "2026-07-31T19:15:41.906358",
        "data": {"invoiceNo": {"value": "FV/2026/01"}},
    }

    invoice = Invoice.from_dict(data)

    assert invoice.history() == [("2026-07-31T19:15:41.906358", "Utworzono")]
