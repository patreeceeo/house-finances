import pytest


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    """Tests run as a local editor unless they set the sign-in variables."""
    for name in ("HOUSEFIN_ACCESS_TEAM_DOMAIN", "HOUSEFIN_ACCESS_AUD", "HOUSEFIN_EDITORS", "HOUSEFIN_ROLE"):
        monkeypatch.delenv(name, raising=False)
