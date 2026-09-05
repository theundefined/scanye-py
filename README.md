# Scanye Python Library and CLI

A Python library and CLI tool for interacting with the Scanye API.

> **Unofficial project.** This library is not created, maintained, sponsored, or endorsed by Scanye. It's a community-built client for the unofficial Scanye API (`api.scanye.pl`), reverse-engineered from the Scanye web app's network traffic. Use at your own risk.

## Features
- Login and authentication
- List sales and purchase invoices
- Filter invoices by KSeF status
- Download invoices as PDF
- CLI for easy access

## Installation
```bash
pip install scanye-py
```

## CLI Usage
First, login to your account:
```bash
scanye login --email your@email.com
```

List your sales invoices:
```bash
scanye invoices list --type sales
```

List purchase invoices that are not yet sent to KSeF:
```bash
scanye invoices list --type purchase --unsent
```

Download all sales and purchase invoices from a given month as PDF (multiple invoices are downloaded as a single ZIP and extracted automatically):
```bash
scanye invoices download --type sales --month 2026-07 -o ./invoices/2026-07/sales
scanye invoices download --type purchase --month 2026-07 -o ./invoices/2026-07/purchase
```

Download a single invoice by ID:
```bash
scanye invoices download <invoice-id> -o ./invoices
```

## Library Usage
```python
from scanye.client import ScanyeClient

client = ScanyeClient()
client.login("your@email.com", "your_password")

invoices = client.fetch_invoices(is_sales=True)
for inv in invoices:
    print(f"{inv.invoice_no}: {inv.ksef_status}")
```
