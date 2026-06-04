import logging
from typing import Any, Dict, List, Optional

import httpx

from .exceptions import ScanyeAuthError, ScanyeError, ScanyeRequestError
from .models import Invoice

logger = logging.getLogger(__name__)


class ScanyeClient:
    BASE_URL = "https://api.scanye.pl"

    def __init__(self, token: Optional[str] = None, debug: bool = False, client_id: Optional[str] = None):
        self.token = token
        self.debug = debug
        self.client_id = client_id
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
            try:
                logger.debug(f"DEBUG: Response Body: {response.json()}")
            except Exception:
                logger.debug(f"DEBUG: Response Text: {response.text}")

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

    def _get_client_id(self) -> str:
        if self.client_id:
            return self.client_id
        info = self.get_info()
        self.client_id = info.get("clientId") or info.get("user", {}).get("clientId")
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

        headers = self._get_headers()
        self._log_request("POST", url, json=data, headers=headers)
        try:
            response = self._client.post(url, json=data, headers=headers)
            self._log_response(response)
            response.raise_for_status()
            res_data = response.json()
            if isinstance(res_data, list):
                return [Invoice.from_dict(item) for item in res_data]
            elif isinstance(res_data, dict) and "items" in res_data:
                return [Invoice.from_dict(item) for item in res_data["items"]]
            return []
        except Exception as e:
            raise ScanyeRequestError(f"Failed to fetch invoices: {e}") from e

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

        headers = self._get_headers()
        self._log_request("POST", url, json=data, headers=headers)
        try:
            response = self._client.post(url, json=data, headers=headers)
            self._log_response(response)
            response.raise_for_status()
            return True
        except Exception as e:
            raise ScanyeRequestError(f"Failed to mark invoices as paid: {e}") from e

    def send_to_ksef(self, invoice_ids: List[str]) -> bool:
        """
        Sends invoices to KSeF.

        :param invoice_ids: List of invoice IDs to send.
        :return: True if successful.
        """
        url = "/operational-invoices/send-to-ksef?context=InvoicePreview"
        headers = self._get_headers()
        self._log_request("POST", url, json=invoice_ids, headers=headers)
        try:
            response = self._client.post(url, json=invoice_ids, headers=headers)
            self._log_response(response)
            response.raise_for_status()
            return True
        except Exception as e:
            raise ScanyeRequestError(f"Failed to send invoices to KSeF: {e}") from e

    def __enter__(self) -> "ScanyeClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._client.close()
