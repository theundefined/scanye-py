# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Scanye is a Python library and CLI (`scanye-py`) for the unofficial Scanye accounting API (`https://api.scanye.pl`). It handles login, listing sales/purchase invoices, marking invoices as paid/unpaid, sending invoices to KSeF (the Polish e-invoicing system), and downloading invoices as PDF.

## Development commands

Set up the environment (a `venv` already exists at the repo root):
```bash
source venv/bin/activate
pip install -e ".[dev]"
```

Run tests:
```bash
pytest
pytest tests/test_client.py::test_login_success  # single test
```

Lint and format:
```bash
ruff check src tests
black src tests
```

Type-check (configured for the `src` directory only, all defs must be typed):
```bash
mypy
```

Run the CLI locally during development:
```bash
python -m scanye.cli invoices list --type sales
# or, if installed in editable mode:
scanye invoices list --type sales --unsent -v
```

Manual/exploratory scripts (not part of the test suite) live at the repo root — `example.py` and `check_info.py`. Both read `SCANYE_EMAIL`/`SCANYE_PASSWORD`/`SCANYE_TOKEN` from a `.env` file (see `.env.example`).

## Architecture

All library code lives in `src/scanye/`:

- **`client.py`** — `ScanyeClient` is the sole HTTP boundary. It wraps a persistent `httpx.Client` against `BASE_URL`, injects auth (`authorization: Scanye <token>`) and browser-like headers on every request, and exposes one method per API operation (`login`, `get_info`, `fetch_invoices`, `mark_as_paid`, `mark_as_unpaid`, `send_to_ksef`, `create_printout`, `get_printout_status`, `download_printout`, `fetch_printout`). Every method catches the underlying `httpx` exception and re-raises as a `Scanye*Error` — callers (CLI or library users) never see raw `httpx` exceptions. `debug=True` turns on request/response logging via the standard `logging` module (the `authorization` header is always scrubbed from debug logs; binary response bodies are summarized as `<N bytes, content-type>` instead of being decoded as text).
- **Auto re-login on expired token** — if constructed with `email`/`password`, the client transparently refreshes an expired token instead of failing: `_authenticated_request()` (used by `fetch_invoices`, `mark_as_paid`, `mark_as_unpaid`, `send_to_ksef`, and the printout methods) retries once via `_reauthenticate()` on an HTTP 401, and `_get_client_id()` does the same when `/auth/info` responds 200 with `authenticated: False` (that endpoint doesn't 401 on a stale token — it reports status in the body instead). Callers must re-check `client.token` after any call, since it may have been silently replaced. `_authenticated_request()` also accepts an `extra_headers` dict, merged on top of the default headers, for endpoints that need extra headers beyond the standard auth/content-type set (e.g. `x-app-context`/`x-page-path` for printouts).
- **Invoice PDF downloads (`/printouts`)** — reverse-engineered from the Scanye web app's network traffic (not in any public API docs). `create_printout(invoice_ids)` `POST`s to `/printouts` and gets back a job ID synchronously (HTTP 202); a single invoice ID produces a `Pdf` job, multiple IDs produce one `Zip` job containing one PDF per invoice (not one job per invoice — the API batches). `get_printout_status(id)` polls `GET /printouts/{id}` for `{"status": ...}`, which the observed API returns as `"Finished"` immediately in every tested case (up to 70 invoices in one batch) — a bad invoice ID fails synchronously on the `create_printout` call (HTTP 500) rather than surfacing as a polled failure status, so there is no confirmed "Failed" status string to check for. `download_printout(id)` then `GET`s `/printouts/{id}/data` for the raw bytes, with the suggested filename parsed out of the `content-disposition` header. `fetch_printout(invoice_ids)` chains all three with a poll loop (`poll_interval`/`timeout` params) and is what the CLI uses. The `x-page-path` header sent with these requests was confirmed to be cosmetic (analytics only) — omitting it or sending the wrong value still returns the correct content — so it's hardcoded rather than threaded through as a parameter.
- **`_get_client_id()`** lazily resolves and caches `client_id` by calling `get_info()` if it wasn't supplied at construction — most invoice operations require it.
- **`models.py`** — `Invoice.from_dict()` is the only place that understands the Scanye API's raw JSON shape. The API wraps many fields as `{"value": ...}` (a `get_val()` helper unwraps these), and KSeF status/reference can appear in three different locations in the payload (`sentToKsef.type`, `ksef.sendingStatus`/`ksef.status`, or inferred from a bare `ksefNo` meaning `"SENT"`) — `from_dict()` checks them in that priority order. Every invoice payload carries both a `payer` (the buyer) and a `payee` (the seller) object regardless of invoice direction — on a sales invoice `payer` is the counterparty (your client) and on a purchase invoice `payee` is (your vendor); the other side is always you. `Invoice.counterparty_name`/`counterparty_tax_no`/`counterparty_email` resolve to whichever side is relevant based on `is_sales`, so callers never need to pick `payer` vs `payee` themselves. The full raw dict is preserved on `Invoice.raw_data` for anything not surfaced as a typed field.
- **`cli.py`** — `argparse`-based CLI, thin over `ScanyeClient`. Credentials are persisted as JSON at `~/.config/scanye/config.json` via `save_config`/`load_config`, with the directory and file locked to `0700`/`0600` on every write (a pre-existing world-readable config from before this was added won't self-heal — it only gets tightened the next time `save_config` runs). `scanye login` always saves the token/email and interactively asks whether to also save the plaintext password; saving it is what enables the client's auto re-login. `build_client(config, debug)` / `persist_token(config, client)` are the shared helpers every command handler uses: build the client from stored token+email+password, run the operation in a `try/finally`, and persist the token in `finally` in case an internal auto re-login silently rotated it — this must happen even on the error path, not just on success. `require_credentials()` gates commands on having either a token or a saved email+password (auth commands no longer require a live token up front). `invoices list --unsent` is filtered client-side after fetching (it over-fetches by 5x via `fetch_limit` to compensate) because "unsent to KSeF" isn't a server-side filter. `invoices send-ksef --all` similarly fetches recent unsent sales invoices itself before sending. `invoices download` mirrors `list`'s month/filter selection logic (both use the shared `build_month_filters()` helper) but takes either explicit invoice IDs or `--month`/`--filter`/`--limit`, not both — mixing them exits with an error. It calls `client.fetch_printout()` and, based on `zipfile.is_zipfile()` on the returned bytes (not the filename extension, which the server doesn't always get right), either extracts a multi-invoice ZIP into `--output` or writes a single PDF there directly. Running `scanye invoices` (or any group) with no subcommand prints that subparser's own help (`invoice_parser.print_help()`), not the top-level parser's — each subcommand group must print its own subparser, not the shared top-level `parser`.
- **`exceptions.py`** — flat hierarchy: `ScanyeError` (base) → `ScanyeAuthError` (401 / bad credentials), `ScanyeRequestError` (everything else, including non-auth HTTP errors and network failures).

## Testing

Tests use `respx` to mock `httpx` calls against the real `BASE_URL` routes rather than mocking `ScanyeClient` internals — `tests/test_client.py` mocks specific endpoints (`/auth/log-in`, `/auth/info`, `/invoices/fetch`, `/printouts`, etc.) and asserts on `ScanyeClient` outputs/exceptions, including the 401/`authenticated: False` auto re-login retries (asserted via respx's `side_effect` list and `.call_count`) and the printout poll loop (mocked with `side_effect` returning a non-`Finished` status then `Finished`, called with `poll_interval=0` so the test doesn't sleep). Binary printout responses are mocked with `httpx.Response(200, content=b"...", headers={"content-type": ..., "content-disposition": ...})`. `tests/test_models.py` tests `Invoice.from_dict()` directly against representative raw API payloads (including all three KSeF-status code paths). `tests/test_cli.py` monkeypatches `cli.CONFIG_DIR`/`cli.CONFIG_FILE` to a `tmp_path` (never touches the real `~/.config/scanye/`) to test config permissions, the login password-save prompt, and `invoices download` (both the single-PDF and ZIP-extraction paths, plus the invoice-IDs-vs-filters conflict). When adding a new client method, follow this same pattern: mock the route with `@respx.mock`, assert on the parsed result or the raised `Scanye*Error`.
