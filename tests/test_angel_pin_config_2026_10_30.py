"""Angel MPIN alias resolution and redaction use dummy credentials only."""
import os

import pytest

from config.settings import Settings, PROTECTED_SECRET_ENV_KEYS
from trading.secure_logging import redact_sensitive_text


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if "ANGELONE" in key.upper() or "ANGEL_ONE" in key.upper():
            monkeypatch.delenv(key)


def test_canonical_dotenv_password_precedes_stale_process_pin(tmp_path, monkeypatch):
    path = tmp_path/"credentials.env"
    path.write_text("ANGELONE_PASSWORD='1111'\n", encoding="utf-8")
    monkeypatch.setenv("ANGELONE_PIN", "2222")
    assert Settings(_env_file=path).angelone_password == "1111"


def test_explicit_process_password_precedes_pin_and_dotenv(tmp_path, monkeypatch):
    path = tmp_path/"credentials.env"
    path.write_text("ANGELONE_PASSWORD='1111'\n", encoding="utf-8")
    monkeypatch.setenv("ANGELONE_PASSWORD", "2222")
    monkeypatch.setenv("ANGELONE_PIN", "3333")
    assert Settings(_env_file=path).angelone_password == "2222"


def test_field_name_constructor_precedes_process_alias(monkeypatch):
    monkeypatch.setenv("ANGELONE_PIN", "2222")
    assert Settings(_env_file=None, angelone_password="1111").angelone_password == "1111"


@pytest.mark.parametrize("alias", ["ANGELONE_PIN", "angelone_pin"])
def test_process_pin_alias_is_case_insensitive(monkeypatch, alias):
    monkeypatch.setenv(alias, "1111")
    assert Settings(_env_file=None).angelone_password == "1111"


def test_dotenv_pin_is_accepted_without_changing_model_field_name(tmp_path):
    path = tmp_path/"credentials.env"
    path.write_text("ANGELONE_PIN='1111'\n", encoding="utf-8")
    settings = Settings(_env_file=path)
    assert settings.angelone_password == "1111"
    assert settings.model_dump()["angelone_password"] == "1111"
    assert "ANGELONE_PIN" not in settings.model_dump()
    assert "angelone_pin" not in settings.model_dump()


@pytest.mark.parametrize("empty_password", ["", "   "])
def test_pin_fills_only_empty_canonical_password(empty_password):
    settings = Settings(_env_file=None, angelone_password=empty_password, angelone_pin="1111")
    assert settings.angelone_password == "1111"


def test_password_alias_does_not_replace_project_api_key_with_unverified_bundle(tmp_path, monkeypatch):
    path = tmp_path/"credentials.env"
    path.write_text("ANGELONE_API_KEY='dummy_project_key'\n", encoding="utf-8")
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "dummy_unverified_key")
    monkeypatch.setenv("ANGELONE_PIN", "1111")
    settings = Settings(_env_file=path)
    assert settings.angelone_api_key == "dummy_project_key"
    assert settings.angelone_password == "1111"


def test_pin_alias_is_protected_from_blank_secret_persistence():
    assert "ANGELONE_PIN" in PROTECTED_SECRET_ENV_KEYS


@pytest.mark.parametrize("message", [
    "pin=123456", "mpin:123456", "ANGELONE_PIN='123456'",
    '{"ANGELONE_PIN":"123456"}', "ANGELONE_PASSWORD=123456",
])
def test_pin_alias_and_mpin_are_redacted(message):
    result = redact_sensitive_text(message)
    assert "123456" not in result
    assert "<redacted>" in result
