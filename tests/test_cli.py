import io
import stat
import zipfile

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
