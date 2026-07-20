from engine import settings_persistence
from engine.settings_persistence import AsyncSettingsPersister
from config.settings import Settings


def test_settings_persistence_retries_transient_permission_error(monkeypatch):
    monkeypatch.setattr(settings_persistence.time, "sleep", lambda _seconds: None)

    class FlakySettings:
        def __init__(self):
            self.calls = 0

        def save_to_env(self):
            self.calls += 1
            if self.calls < 3:
                raise PermissionError("locked")

    settings = FlakySettings()
    persister = AsyncSettingsPersister(delay_seconds=0.01)

    persister.schedule(settings, immediate=True)

    assert settings.calls == 3
    assert persister.save_count == 1
    assert persister.status()["pending"] is False
    assert persister.last_error == ""


def test_settings_persistence_keeps_pending_after_locked_file(monkeypatch):
    monkeypatch.setattr(settings_persistence.time, "sleep", lambda _seconds: None)

    class LockedSettings:
        def __init__(self):
            self.calls = 0

        def save_to_env(self):
            self.calls += 1
            raise PermissionError("locked")

    settings = LockedSettings()
    persister = AsyncSettingsPersister(delay_seconds=0.01)

    persister.schedule(settings, immediate=True)

    assert settings.calls == 5
    assert persister.save_count == 0
    assert persister.status()["pending"] is True
    assert "locked" in persister.last_error


def test_save_to_env_preserves_existing_broker_secrets_when_current_values_are_blank(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "ANGELONE_API_KEY='old_api'",
                "ANGELONE_PASSWORD='old_pin'",
                "ANGELONE_TOTP_SECRET='old_totp'",
                "ANGELONE_SECRET_KEY='old_secret'",
                "FYERS_SECRET_KEY='old_fyers_secret'",
                "FYERS_PASSWORD='old_fyers_pin'",
                "TRADING_MODE='REAL'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        angelone_api_key="",
        angelone_password="",
        angelone_totp_secret="",
        angelone_secret_key="",
        fyers_secret_key="",
        fyers_password="",
        trading_mode="HISTORICAL",
    )

    settings.save_to_env()
    saved = env_file.read_text(encoding="utf-8")

    assert "ANGELONE_API_KEY='old_api'" in saved
    assert "ANGELONE_PASSWORD='old_pin'" in saved
    assert "ANGELONE_TOTP_SECRET='old_totp'" in saved
    assert "ANGELONE_SECRET_KEY='old_secret'" in saved
    assert "FYERS_SECRET_KEY='old_fyers_secret'" in saved
    assert "FYERS_PASSWORD='old_fyers_pin'" in saved
    assert "TRADING_MODE='HISTORICAL'" in saved
