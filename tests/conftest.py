import pytest


@pytest.fixture(autouse=True)
def _config_path(tmp_path, monkeypatch):
    # Bug this guards: a test run reading (or writing) a real /data/config.json, so
    # results depend on the machine. Every app built from the env gets a fresh path.
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config" / "config.json"))
