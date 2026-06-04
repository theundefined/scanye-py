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
    assert invoice.payer_name == "Test Payer"
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
