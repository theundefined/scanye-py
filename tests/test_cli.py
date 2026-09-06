import io
import json
import stat
import zipfile
from datetime import datetime

import httpx
import pytest
import respx

from scanye import cli
from scanye.models import Invoice


def _make_invoice(invoice_no: str, issue_date: str) -> Invoice:
    return Invoice.from_dict(
        {
            "id": invoice_no,
            "data": {
                "invoiceNo": {"value": invoice_no},
                "accounting": {"sales": {"value": "true"}},
                "payer": {"name": {"value": "Test"}},
                "dates": {"issue": {"value": issue_date}},
            },
        }
    )


def test_invoice_sort_key_orders_by_date_then_numeric_invoice_number():
    # Invoice numbers with the same date must sort numerically ("10" after "2"),
    # not lexicographically, and dates take priority over invoice numbers.
    invoices = [
        _make_invoice("FV/26/09/1", "01.09.2026"),
        _make_invoice("FV/26/09/10", "01.09.2026"),
        _make_invoice("FV/26/09/2", "01.09.2026"),
        _make_invoice("FV/26/08/9", "31.08.2026"),
    ]

    invoices.sort(key=cli._invoice_sort_key, reverse=True)

    assert [inv.invoice_no for inv in invoices] == [
        "FV/26/09/10",
        "FV/26/09/2",
        "FV/26/09/1",
        "FV/26/08/9",
    ]


def test_trim_to_full_days_returns_all_when_under_limit():
    invoices = [_make_invoice("FV/1", "02.09.2026"), _make_invoice("FV/2", "01.09.2026")]

    assert cli._trim_to_full_days(invoices, 5) == invoices


def test_trim_to_full_days_keeps_limit_when_boundary_is_a_full_day():
    invoices = [
        _make_invoice("FV/1", "02.09.2026"),
        _make_invoice("FV/2", "01.09.2026"),
        _make_invoice("FV/3", "31.08.2026"),
    ]

    assert [inv.invoice_no for inv in cli._trim_to_full_days(invoices, 2)] == ["FV/1", "FV/2"]


def test_trim_to_full_days_drops_partial_day_at_the_boundary():
    invoices = [
        _make_invoice("FV/1", "02.09.2026"),
        _make_invoice("FV/2", "01.09.2026"),
        _make_invoice("FV/3", "01.09.2026"),
        _make_invoice("FV/4", "31.08.2026"),
    ]

    # limit=2 would otherwise cut FV/2 and FV/3 (both 01.09.2026) in half -- drop the whole day.
    assert [inv.invoice_no for inv in cli._trim_to_full_days(invoices, 2)] == ["FV/1"]


def test_trim_to_full_days_falls_back_to_raw_slice_if_trimming_empties_it():
    invoices = [
        _make_invoice("FV/1", "01.09.2026"),
        _make_invoice("FV/2", "01.09.2026"),
        _make_invoice("FV/3", "01.09.2026"),
    ]

    # Every fetched invoice shares the boundary date -- trimming it away would leave nothing,
    # so fall back to the plain slice rather than showing an empty list.
    assert [inv.invoice_no for inv in cli._trim_to_full_days(invoices, 2)] == ["FV/1", "FV/2"]


def test_save_config_sets_restrictive_permissions(tmp_path, monkeypatch):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")

    cli.save_config({"token": "abc"})

    dir_mode = stat.S_IMODE(config_dir.stat().st_mode)
    file_mode = stat.S_IMODE((config_dir / "config.json").stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600
    assert cli.load_config() == {"token": "abc"}


def test_handle_login_saves_password_when_confirmed(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.setattr(cli, "getpass", lambda prompt: "secret-password")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    with respx.mock:
        respx.post("https://api.scanye.pl/auth/log-in").mock(
            return_value=httpx.Response(200, json={"apiKey": "fresh-token"})
        )
        args = type("Args", (), {"email": "test@example.com", "debug": False})()
        cli.handle_login(args)

    config = cli.load_config()
    assert config == {"token": "fresh-token", "email": "test@example.com", "password": "secret-password"}


def test_handle_login_skips_password_when_declined(tmp_path, monkeypatch):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    monkeypatch.setattr(cli, "getpass", lambda prompt: "secret-password")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    with respx.mock:
        respx.post("https://api.scanye.pl/auth/log-in").mock(
            return_value=httpx.Response(200, json={"apiKey": "fresh-token"})
        )
        args = type("Args", (), {"email": "test@example.com", "debug": False})()
        cli.handle_login(args)

    config = cli.load_config()
    assert config == {"token": "fresh-token", "email": "test@example.com"}


def test_persist_token_saves_only_on_change(tmp_path, monkeypatch):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")

    config = {"token": "old-token", "email": "test@example.com", "password": "pw"}
    client = cli.build_client(config, debug=False)

    client.token = "old-token"
    cli.persist_token(config, client)
    assert not (config_dir / "config.json").exists()

    client.token = "new-token"
    cli.persist_token(config, client)
    assert cli.load_config()["token"] == "new-token"


def test_handle_invoices_send_email_success(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    with respx.mock:
        route = respx.post(
            "https://api.scanye.pl/operational-invoices/invoice-1/send-to-buyer?context=InvoicesList"
        ).mock(return_value=httpx.Response(200, json={}))

        args = type(
            "Args",
            (),
            {"invoice_id": "invoice-1", "to": "buyer@example.com", "no_save_email": False, "debug": False},
        )()
        cli.handle_invoices_send_email(args)

    assert route.called
    assert "Successfully sent invoice invoice-1 to buyer@example.com" in capsys.readouterr().out


def _list_args(type_="sales", limit=None, unsent=False, month=None, filter_=None, verbose=False):
    return type(
        "Args",
        (),
        {
            "type": type_,
            "limit": limit,
            "unsent": unsent,
            "month": month,
            "filter": filter_,
            "verbose": verbose,
            "debug": False,
        },
    )()


def test_handle_invoices_list_defaults_to_current_month(tmp_path, monkeypatch):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 15)

    monkeypatch.setattr(cli, "datetime", FixedDatetime)

    with respx.mock:
        respx.get("https://api.scanye.pl/auth/info").mock(
            return_value=httpx.Response(200, json={"clientId": "test-client-id"})
        )
        route = respx.post("https://api.scanye.pl/invoices/fetch").mock(return_value=httpx.Response(200, json=[]))

        cli.handle_invoices_list(_list_args())

    body = json.loads(route.calls.last.request.content)
    assert "annotations.accountingMonth>=2026-09" in body["filter"]
    assert "annotations.accountingMonth<=2026-09" in body["filter"]


def test_handle_invoices_list_no_limit_shows_everything_fetched(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    mock_data = [
        {
            "id": f"inv-{i}",
            "data": {
                "invoiceNo": {"value": f"FV/{i}"},
                "accounting": {"sales": {"value": "true"}},
                "payer": {"name": {"value": "Test"}},
                "dates": {"issue": {"value": "01.09.2026"}},
            },
        }
        for i in range(15)
    ]

    with respx.mock:
        respx.get("https://api.scanye.pl/auth/info").mock(
            return_value=httpx.Response(200, json={"clientId": "test-client-id"})
        )
        respx.post("https://api.scanye.pl/invoices/fetch").mock(return_value=httpx.Response(200, json=mock_data))

        cli.handle_invoices_list(_list_args())

    out = capsys.readouterr().out
    for i in range(15):
        assert f"FV/{i}" in out


def test_handle_invoices_show_prints_details_and_history(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    with respx.mock:
        respx.get("https://api.scanye.pl/invoices/invoice-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "invoice-1",
                    "dateCreated": "2026-07-31T19:15:41.906358",
                    "data": {
                        "invoiceNo": {"value": "FV/2026/01"},
                        "accounting": {"sales": {"value": "true"}},
                        "payer": {"name": {"value": "Test Buyer"}},
                    },
                },
            )
        )

        args = type("Args", (), {"invoice_id": "invoice-1", "debug": False})()
        cli.handle_invoices_show(args)

    out = capsys.readouterr().out
    assert "FV/2026/01" in out
    assert "Test Buyer" in out
    assert "Utworzono" in out


def test_handle_invoices_show_not_found(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    with respx.mock:
        respx.get("https://api.scanye.pl/invoices/missing-id").mock(
            return_value=httpx.Response(404, json={"message": "Invoice 'missing-id' not found"})
        )

        args = type("Args", (), {"invoice_id": "missing-id", "debug": False})()
        with pytest.raises(SystemExit):
            cli.handle_invoices_show(args)

    assert "not found" in capsys.readouterr().err


def _download_args(tmp_path, invoice_ids=None, month=None, output=None):
    return type(
        "Args",
        (),
        {
            "invoice_ids": invoice_ids or [],
            "month": month,
            "filter": None,
            "type": "sales",
            "limit": 100,
            "output": output or str(tmp_path / "out"),
            "debug": False,
        },
    )()


def test_handle_invoices_download_specific_id_saves_pdf(tmp_path, monkeypatch):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    with respx.mock:
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

        args = _download_args(tmp_path, invoice_ids=["invoice-1"])
        cli.handle_invoices_download(args)

    saved = tmp_path / "out" / "invoice_123.pdf"
    assert saved.read_bytes() == b"%PDF-1.4 fake content"


def test_handle_invoices_download_by_month_extracts_zip(tmp_path, monkeypatch):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("invoice_1.pdf", b"pdf-1")
        zf.writestr("invoice_2.pdf", b"pdf-2")

    mock_invoices = [
        {"id": "invoice-1", "data": {"invoiceNo": {"value": "INV-1"}}},
        {"id": "invoice-2", "data": {"invoiceNo": {"value": "INV-2"}}},
    ]

    with respx.mock:
        respx.get("https://api.scanye.pl/auth/info").mock(
            return_value=httpx.Response(200, json={"clientId": "test-client-id"})
        )
        respx.post("https://api.scanye.pl/invoices/fetch").mock(return_value=httpx.Response(200, json=mock_invoices))
        respx.post("https://api.scanye.pl/printouts").mock(return_value=httpx.Response(202, json="printout-id"))
        respx.get("https://api.scanye.pl/printouts/printout-id").mock(
            return_value=httpx.Response(200, json={"id": "printout-id", "status": "Finished", "fileType": "Zip"})
        )
        respx.get("https://api.scanye.pl/printouts/printout-id/data").mock(
            return_value=httpx.Response(
                200,
                content=zip_buffer.getvalue(),
                headers={
                    "content-type": "application/zip",
                    "content-disposition": 'attachment; filename="Dokumenty.zip"',
                },
            )
        )

        args = _download_args(tmp_path, month=["2026-07"])
        cli.handle_invoices_download(args)

    assert (tmp_path / "out" / "invoice_1.pdf").read_bytes() == b"pdf-1"
    assert (tmp_path / "out" / "invoice_2.pdf").read_bytes() == b"pdf-2"


def test_handle_invoices_download_rejects_ids_with_month(tmp_path, monkeypatch):
    config_dir = tmp_path / "scanye"
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cli, "CONFIG_FILE", config_dir / "config.json")
    cli.save_config({"token": "test-token"})

    args = _download_args(tmp_path, invoice_ids=["invoice-1"], month=["2026-07"])
    with pytest.raises(SystemExit):
        cli.handle_invoices_download(args)
