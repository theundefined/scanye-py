import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .exceptions import ScanyeAuthError, ScanyeError, ScanyeRequestError
from .models import Invoice

logger = logging.getLogger(__name__)


def _parse_filename(content_disposition: str) -> Optional[str]:
    match = re.search(r'filename="?([^";]+)"?', content_disposition)
    return match.group(1) if match else None


def _parse_invoice_list(res_data: Any) -> List[Invoice]:
    if isinstance(res_data, list):
        return [Invoice.from_dict(item) for item in res_data]
    elif isinstance(res_data, dict) and "items" in res_data:
        return [Invoice.from_dict(item) for item in res_data["items"]]
    return []


class ScanyeClient:
    BASE_URL = "https://api.scanye.pl"

    def __init__(
        self,
        token: Optional[str] = None,
        debug: bool = False,
        client_id: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.token = token
        self.debug = debug
        self.client_id = client_id
        self.email = email
        self.password = password
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=30.0)
        if self.debug:
            logging.basicConfig(level=logging.DEBUG)
            logger.setLevel(logging.DEBUG)

    def _log_request(self, method: str, url: str, **kwargs: Any) -> None:
        if self.debug:
            logger.debug(f"DEBUG: Request: {method} {url}")
            if "json" in kwargs:
                logger.debug(f"DEBUG: Request Body: {kwargs['json']}")
            if "headers" in kwargs:
                # Filter out sensitive authorization header for cleaner but safe logs
                safe_headers = {k: v for k, v in kwargs["headers"].items() if k.lower() != "authorization"}
                logger.debug(f"DEBUG: Request Headers: {safe_headers}")

    def _log_response(self, response: httpx.Response) -> None:
        if self.debug:
            logger.debug(f"DEBUG: Response Status: {response.status_code}")
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                logger.debug(f"DEBUG: Response Body: {response.json()}")
            elif "text" in content_type:
                logger.debug(f"DEBUG: Response Text: {response.text}")
            else:
                logger.debug(f"DEBUG: Response Body: <{len(response.content)} bytes, {content_type or 'unknown type'}>")

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://app.scanye.pl",
            "referer": "https://app.scanye.pl/",
        }
        if self.token:
            headers["authorization"] = f"Scanye {self.token}"
        return headers

    def _reauthenticate(self) -> bool:
        """Attempt to obtain a fresh token using stored credentials. Returns True on success."""
        if not self.email or not self.password:
            return False
        try:
            self.login(self.email, self.password)
            return True
        except ScanyeError:
            return False

    def _authenticated_request(
        self, method: str, url: str, extra_headers: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> httpx.Response:
        headers = self._get_headers()
        if extra_headers:
            headers.update(extra_headers)
        self._log_request(method, url, headers=headers, **kwargs)
        response = self._client.request(method, url, headers=headers, **kwargs)
        self._log_response(response)

        if response.status_code == 401 and self._reauthenticate():
            headers = self._get_headers()
            if extra_headers:
                headers.update(extra_headers)
            self._log_request(method, url, headers=headers, **kwargs)
            response = self._client.request(method, url, headers=headers, **kwargs)
            self._log_response(response)

        return response

    def _get_client_id(self) -> str:
        if self.client_id:
            return self.client_id
        info = self.get_info()
        if info.get("authenticated") is False and self._reauthenticate():
            info = self.get_info()
        client_id = info.get("clientId") or (info.get("user") or {}).get("clientId")
        self.client_id = client_id if isinstance(client_id, str) else None
        if not self.client_id:
            raise ScanyeError("Could not determine clientId from user info")
        return self.client_id

    def login(self, email: str, password: str) -> str:
        url = "/auth/log-in"
        data = {"email": email, "password": password, "purpose": "web"}
        headers = self._get_headers()

        self._log_request("POST", url, json=data, headers=headers)
        try:
            response = self._client.post(url, json=data, headers=headers)
            self._log_response(response)
            response.raise_for_status()

            res_data = response.json()
            token = res_data.get("apiKey")
            if not token or not isinstance(token, str):
                raise ScanyeAuthError("Token (apiKey) not found in login response")
            self.token = token

            # Try to get clientId from login response if available
            self.client_id = res_data.get("clientId")

            return token
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ScanyeAuthError("Invalid credentials") from e
            raise ScanyeRequestError(f"Login failed: {e}") from e
        except Exception as e:
            raise ScanyeRequestError(f"An error occurred during login: {e}") from e

    def get_info(self) -> Dict[str, Any]:
        url = "/auth/info"
        headers = self._get_headers()
        self._log_request("GET", url, headers=headers)
        try:
            response = self._client.get(url, headers=headers)
            self._log_response(response)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
            return {}
        except Exception as e:
            raise ScanyeRequestError(f"Failed to get user info: {e}") from e

    def fetch_invoices(
        self,
        is_sales: bool = True,
        limit: int = 100,
        offset: int = 0,
        filters: Optional[str] = None,
        sort: str = "-data.dates.issue",
        months: Optional[List[str]] = None,
        client_ids: Optional[List[str]] = None,
    ) -> List[Invoice]:
        url = "/invoices/fetch"

        if not client_ids:
            client_ids = [self._get_client_id()]

        base_filter = f"data.accounting.sales={'true' if is_sales else 'false'}"
        if filters:
            combined_filter = f"{base_filter},{filters}"
        else:
            combined_filter = base_filter

        data: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "filter": combined_filter,
            "clientIds": client_ids,
        }

        if months:
            data["months"] = months

        try:
            response = self._authenticated_request("POST", url, json=data)
            response.raise_for_status()
            return _parse_invoice_list(response.json())
        except Exception as e:
            raise ScanyeRequestError(f"Failed to fetch invoices: {e}") from e

    def get_invoice(self, invoice_id: str) -> Optional[Invoice]:
        """
        Fetches a single invoice by ID, regardless of whether it's a sales or purchase invoice.

        :param invoice_id: ID of the invoice to fetch.
        :return: The invoice, or None if no invoice with that ID exists.
        """
        url = "/invoices/fetch"
        data = {
            "limit": 1,
            "offset": 0,
            "sort": "",
            "filter": f"id={invoice_id}",
            "clientIds": [self._get_client_id()],
        }

        try:
            response = self._authenticated_request("POST", url, json=data)
            response.raise_for_status()
            invoices = _parse_invoice_list(response.json())
            return invoices[0] if invoices else None
        except Exception as e:
            raise ScanyeRequestError(f"Failed to fetch invoice {invoice_id}: {e}") from e

    def mark_as_paid(self, invoice_ids: List[str], transfer_date: Optional[str] = None) -> bool:
        """
        Marks invoices as paid by setting the transfer order date.

        :param invoice_ids: List of invoice IDs to mark as paid.
        :param transfer_date: Date of payment (YYYY-MM-DD). Defaults to today.
        :return: True if successful.
        """
        url = "/invoices/order-transfer-date"
        if not transfer_date:
            from datetime import datetime

            transfer_date = datetime.now().strftime("%Y-%m-%d")

        data = {"transferDate": transfer_date, "invoiceIds": invoice_ids}

        try:
            response = self._authenticated_request("POST", url, json=data)
            response.raise_for_status()
            return True
        except Exception as e:
            raise ScanyeRequestError(f"Failed to mark invoices as paid: {e}") from e

    def mark_as_unpaid(self, invoice_ids: List[str]) -> bool:
        """
        Marks invoices as unpaid by removing the transfer order date.

        :param invoice_ids: List of invoice IDs to mark as unpaid.
        :return: True if successful.
        """
        url = "/invoices/order-transfer-date"
        data = {"invoiceIds": invoice_ids}

        try:
            response = self._authenticated_request("POST", url, json=data)
            response.raise_for_status()
            return True
        except Exception as e:
            raise ScanyeRequestError(f"Failed to mark invoices as unpaid: {e}") from e

    def send_to_ksef(self, invoice_ids: List[str]) -> bool:
        """
        Sends invoices to KSeF.

        :param invoice_ids: List of invoice IDs to send.
        :return: True if successful.
        """
        url = "/operational-invoices/send-to-ksef?context=InvoicePreview"

        try:
            response = self._authenticated_request("POST", url, json=invoice_ids)
            response.raise_for_status()
            return True
        except Exception as e:
            raise ScanyeRequestError(f"Failed to send invoices to KSeF: {e}") from e

    def send_to_buyer(self, invoice_id: str, recipient: str, save_email: bool = True) -> bool:
        """
        Sends an invoice to the buyer by e-mail.

        :param invoice_id: ID of the invoice to send.
        :param recipient: Recipient e-mail address.
        :param save_email: Whether to remember this address for the invoice's counterparty.
        :return: True if successful.
        """
        url = f"/operational-invoices/{invoice_id}/send-to-buyer?context=InvoicesList"
        extra_headers = {"x-page-path": "/sales-invoices"}
        data = {"recipient": recipient, "saveEmail": save_email}

        try:
            response = self._authenticated_request("POST", url, json=data, extra_headers=extra_headers)
            response.raise_for_status()
            return True
        except Exception as e:
            raise ScanyeRequestError(f"Failed to send invoice to buyer: {e}") from e

    def create_printout(self, invoice_ids: List[str]) -> str:
        """
        Requests generation of a printable PDF (single invoice) or ZIP (multiple invoices)
        for the given invoice IDs. Returns the printout job ID.
        """
        url = "/printouts"
        extra_headers = {"x-app-context": "InvoicesPage", "x-page-path": "/sales-invoices"}

        try:
            response = self._authenticated_request("POST", url, json=invoice_ids, extra_headers=extra_headers)
            response.raise_for_status()
            printout_id = response.json()
            if not isinstance(printout_id, str):
                raise ScanyeRequestError("Unexpected response when creating printout")
            return printout_id
        except ScanyeError:
            raise
        except Exception as e:
            raise ScanyeRequestError(f"Failed to create printout: {e}") from e

    def get_printout_status(self, printout_id: str) -> Dict[str, Any]:
        """Returns the status of a printout job, e.g. {"status": "Finished", "fileType": "Pdf"}."""
        url = f"/printouts/{printout_id}"
        extra_headers = {"x-page-path": "/sales-invoices"}

        try:
            response = self._authenticated_request("GET", url, extra_headers=extra_headers)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            raise ScanyeRequestError(f"Failed to get printout status: {e}") from e

    def download_printout(self, printout_id: str) -> Tuple[bytes, str]:
        """Downloads the finished printout's content. Returns (content, filename)."""
        url = f"/printouts/{printout_id}/data"
        extra_headers = {"x-page-path": "/sales-invoices"}

        try:
            response = self._authenticated_request("GET", url, extra_headers=extra_headers)
            response.raise_for_status()
            filename = _parse_filename(response.headers.get("content-disposition", "")) or f"{printout_id}.bin"
            return response.content, filename
        except Exception as e:
            raise ScanyeRequestError(f"Failed to download printout: {e}") from e

    def fetch_printout(
        self, invoice_ids: List[str], poll_interval: float = 1.0, timeout: float = 60.0
    ) -> Tuple[bytes, str]:
        """
        Creates a printout job for the given invoices, waits for it to finish, and downloads
        its content. Returns (content, filename) — a PDF for a single invoice, a ZIP of PDFs
        for multiple.
        """
        if not invoice_ids:
            raise ScanyeRequestError("No invoice IDs provided for printout")

        printout_id = self.create_printout(invoice_ids)

        deadline = time.monotonic() + timeout
        status = self.get_printout_status(printout_id)
        while status.get("status") != "Finished":
            if time.monotonic() >= deadline:
                raise ScanyeRequestError(f"Timed out waiting for printout {printout_id} to finish")
            time.sleep(poll_interval)
            status = self.get_printout_status(printout_id)

        return self.download_printout(printout_id)

    def __enter__(self) -> "ScanyeClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._client.close()
