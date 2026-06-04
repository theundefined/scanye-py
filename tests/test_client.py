import httpx
import pytest
import respx

from scanye.client import ScanyeClient
from scanye.exceptions import ScanyeAuthError


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
