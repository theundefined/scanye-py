import json

import httpx
import pytest
import respx

from scanye.client import ScanyeClient
from scanye.exceptions import ScanyeAuthError, ScanyeRequestError


@respx.mock
def test_login_success():
    respx.post("https://api.scanye.pl/auth/log-in").mock(
        return_value=httpx.Response(200, json={"apiKey": "test-token"})
    )

    client = ScanyeClient()
    token = client.login("test@example.com", "password")

    assert token == "test-token"
    assert client.token == "test-token"


@respx.mock
def test_login_failure():
    respx.post("https://api.scanye.pl/auth/log-in").mock(return_value=httpx.Response(401))

    client = ScanyeClient()
    with pytest.raises(ScanyeAuthError):
        client.login("test@example.com", "wrong-password")


@respx.mock
def test_fetch_invoices_success():
    respx.get("https://api.scanye.pl/auth/info").mock(
        return_value=httpx.Response(200, json={"clientId": "test-client-id"})
    )
    mock_data = [
        {
            "id": "1",
            "data": {
                "invoiceNo": {"value": "INV-1"},
                "accounting": {"sales": {"value": "true"}},
                "payer": {"name": {"value": "Payer 1"}},
            },
            "sentToKsef": {"type": "SENT", "ksefReferenceNumber": "REF-1"},
        }
    ]
    respx.post("https://api.scanye.pl/invoices/fetch").mock(return_value=httpx.Response(200, json=mock_data))

    client = ScanyeClient(token="test-token")
    invoices = client.fetch_invoices(is_sales=True)

    assert len(invoices) == 1
    assert invoices[0].invoice_no == "INV-1"
    assert invoices[0].ksef_status == "SENT"


@respx.mock
def test_get_invoice_success():
    respx.get("https://api.scanye.pl/auth/info").mock(
        return_value=httpx.Response(200, json={"clientId": "test-client-id"})
    )
    mock_data = [
        {
            "id": "invoice-1",
            "data": {
                "invoiceNo": {"value": "INV-1"},
                "accounting": {"sales": {"value": "true"}},
                "payer": {"name": {"value": "Payer 1"}},
            },
        }
    ]
    route = respx.post("https://api.scanye.pl/invoices/fetch").mock(return_value=httpx.Response(200, json=mock_data))

    client = ScanyeClient(token="test-token")
    invoice = client.get_invoice("invoice-1")

    assert invoice is not None
    assert invoice.id == "invoice-1"
    assert json.loads(route.calls.last.request.content)["filter"] == "id=invoice-1"


@respx.mock
def test_get_invoice_not_found():
    respx.get("https://api.scanye.pl/auth/info").mock(
        return_value=httpx.Response(200, json={"clientId": "test-client-id"})
    )
    respx.post("https://api.scanye.pl/invoices/fetch").mock(return_value=httpx.Response(200, json=[]))

    client = ScanyeClient(token="test-token")
    invoice = client.get_invoice("missing-id")

    assert invoice is None


@respx.mock
def test_mark_as_paid_success():
    respx.post("https://api.scanye.pl/invoices/order-transfer-date").mock(return_value=httpx.Response(200, json={}))

    client = ScanyeClient(token="test-token")
    result = client.mark_as_paid(["123"], transfer_date="2026-05-08")

    assert result is True


@respx.mock
def test_mark_as_unpaid_success():
    respx.post("https://api.scanye.pl/invoices/order-transfer-date").mock(return_value=httpx.Response(200, json={}))

    client = ScanyeClient(token="test-token")
    result = client.mark_as_unpaid(["123"])

    assert result is True


@respx.mock
def test_send_to_ksef_success():
    respx.post("https://api.scanye.pl/operational-invoices/send-to-ksef?context=InvoicePreview").mock(
        return_value=httpx.Response(200, json={})
    )

    client = ScanyeClient(token="test-token")
    result = client.send_to_ksef(["123", "456"])

    assert result is True


@respx.mock
def test_send_to_buyer_success():
    route = respx.post("https://api.scanye.pl/operational-invoices/inv-1/send-to-buyer?context=InvoicesList").mock(
        return_value=httpx.Response(200, json={})
    )

    client = ScanyeClient(token="test-token")
    result = client.send_to_buyer("inv-1", "buyer@example.com")

    assert result is True
    assert route.calls.last.request.headers["x-page-path"] == "/sales-invoices"
    assert json.loads(route.calls.last.request.content) == {"recipient": "buyer@example.com", "saveEmail": True}


@respx.mock
def test_mark_as_paid_reauthenticates_on_expired_token():
    login_route = respx.post("https://api.scanye.pl/auth/log-in").mock(
        return_value=httpx.Response(200, json={"apiKey": "fresh-token"})
    )
    mark_paid_route = respx.post("https://api.scanye.pl/invoices/order-transfer-date").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={})]
    )

    client = ScanyeClient(token="stale-token", email="test@example.com", password="password")
    result = client.mark_as_paid(["123"], transfer_date="2026-05-08")

    assert result is True
    assert client.token == "fresh-token"
    assert login_route.call_count == 1
    assert mark_paid_route.call_count == 2


@respx.mock
def test_mark_as_paid_gives_up_without_stored_credentials():
    respx.post("https://api.scanye.pl/invoices/order-transfer-date").mock(return_value=httpx.Response(401))

    client = ScanyeClient(token="stale-token")
    with pytest.raises(Exception):
        client.mark_as_paid(["123"], transfer_date="2026-05-08")


@respx.mock
def test_get_client_id_reauthenticates_when_not_authenticated():
    info_route = respx.get("https://api.scanye.pl/auth/info").mock(
        side_effect=[
            httpx.Response(200, json={"authenticated": False, "user": None}),
            httpx.Response(200, json={"clientId": "fresh-client-id"}),
        ]
    )
    respx.post("https://api.scanye.pl/auth/log-in").mock(
        return_value=httpx.Response(200, json={"apiKey": "fresh-token"})
    )

    client = ScanyeClient(token="stale-token", email="test@example.com", password="password")
    client_id = client._get_client_id()

    assert client_id == "fresh-client-id"
    assert info_route.call_count == 2


@respx.mock
def test_create_printout_success():
    respx.post("https://api.scanye.pl/printouts").mock(return_value=httpx.Response(202, json="printout-id"))

    client = ScanyeClient(token="test-token")
    printout_id = client.create_printout(["invoice-1"])

    assert printout_id == "printout-id"


@respx.mock
def test_get_printout_status_success():
    respx.get("https://api.scanye.pl/printouts/printout-id").mock(
        return_value=httpx.Response(200, json={"id": "printout-id", "status": "Finished", "fileType": "Pdf"})
    )

    client = ScanyeClient(token="test-token")
    status = client.get_printout_status("printout-id")

    assert status["status"] == "Finished"
    assert status["fileType"] == "Pdf"


@respx.mock
def test_download_printout_success():
    respx.get("https://api.scanye.pl/printouts/printout-id/data").mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-1.4 fake content",
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="invoice_123.pdf"',
            },
        )
    )

    client = ScanyeClient(token="test-token")
    content, filename = client.download_printout("printout-id")

    assert content == b"%PDF-1.4 fake content"
    assert filename == "invoice_123.pdf"


@respx.mock
def test_fetch_printout_success_single_pdf():
    respx.post("https://api.scanye.pl/printouts").mock(return_value=httpx.Response(202, json="printout-id"))
    respx.get("https://api.scanye.pl/printouts/printout-id").mock(
        return_value=httpx.Response(200, json={"id": "printout-id", "status": "Finished", "fileType": "Pdf"})
    )
    respx.get("https://api.scanye.pl/printouts/printout-id/data").mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-1.4 fake content",
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="invoice_123.pdf"',
            },
        )
    )

    client = ScanyeClient(token="test-token")
    content, filename = client.fetch_printout(["invoice-1"])

    assert content == b"%PDF-1.4 fake content"
    assert filename == "invoice_123.pdf"


@respx.mock
def test_fetch_printout_polls_until_finished():
    respx.post("https://api.scanye.pl/printouts").mock(return_value=httpx.Response(202, json="printout-id"))
    status_route = respx.get("https://api.scanye.pl/printouts/printout-id").mock(
        side_effect=[
            httpx.Response(200, json={"id": "printout-id", "status": "Pending", "fileType": "Zip"}),
            httpx.Response(200, json={"id": "printout-id", "status": "Finished", "fileType": "Zip"}),
        ]
    )
    respx.get("https://api.scanye.pl/printouts/printout-id/data").mock(
        return_value=httpx.Response(
            200,
            content=b"PK\x03\x04 fake zip",
            headers={
                "content-type": "application/zip",
                "content-disposition": 'attachment; filename="Dokumenty.zip"',
            },
        )
    )

    client = ScanyeClient(token="test-token")
    content, filename = client.fetch_printout(["invoice-1", "invoice-2"], poll_interval=0)

    assert filename == "Dokumenty.zip"
    assert status_route.call_count == 2


@respx.mock
def test_fetch_printout_times_out():
    respx.post("https://api.scanye.pl/printouts").mock(return_value=httpx.Response(202, json="printout-id"))
    respx.get("https://api.scanye.pl/printouts/printout-id").mock(
        return_value=httpx.Response(200, json={"id": "printout-id", "status": "Pending", "fileType": "Pdf"})
    )

    client = ScanyeClient(token="test-token")
    with pytest.raises(ScanyeRequestError):
        client.fetch_printout(["invoice-1"], poll_interval=0, timeout=0)


@respx.mock
def test_create_printout_server_error():
    respx.post("https://api.scanye.pl/printouts").mock(return_value=httpx.Response(500, text="Internal server error"))

    client = ScanyeClient(token="test-token")
    with pytest.raises(ScanyeRequestError):
        client.create_printout(["invoice-1"])
