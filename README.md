# Scanye Python Library and CLI

A Python library and CLI tool for interacting with the Scanye API.

## Features
- Login and authentication
- List sales and purchase invoices
- Filter invoices by KSeF status
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

## Library Usage
```python
from scanye.client import ScanyeClient

client = ScanyeClient()
client.login("your@email.com", "your_password")

invoices = client.fetch_invoices(is_sales=True)
for inv in invoices:
    print(f"{inv.invoice_no}: {inv.ksef_status}")
```
