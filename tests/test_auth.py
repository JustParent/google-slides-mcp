"""Auth tests: env-var resolution and non-interactive failure modes.

No network or browser is touched — the interactive consent flow is never invoked
here.
"""

import pytest

from google_slides_mcp import auth

_ENV_VARS = [
    "GOOGLE_CLIENT_SECRET",
    "CREDENTIALS_PATH",
    "GOOGLE_TOKEN_PATH",
    "TOKEN_PATH",
    "GOOGLE_SLIDES_SCOPES",
    "GOOGLE_SLIDES_NO_BROWSER_AUTH",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_client_secret_path_defaults(monkeypatch):
    assert auth._client_secret_path().name == "client_secret.json"


def test_client_secret_path_aliases(monkeypatch):
    monkeypatch.setenv("CREDENTIALS_PATH", "/tmp/creds.json")
    assert str(auth._client_secret_path()) == "/tmp/creds.json"
    # GOOGLE_CLIENT_SECRET wins when both are set.
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "/tmp/secret.json")
    assert str(auth._client_secret_path()) == "/tmp/secret.json"


def test_token_path_aliases(monkeypatch):
    monkeypatch.setenv("TOKEN_PATH", "/tmp/token.json")
    assert str(auth._token_path()) == "/tmp/token.json"
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "/tmp/other.json")
    assert str(auth._token_path()) == "/tmp/other.json"


def test_get_scopes_default_is_full_urls():
    assert auth.get_scopes() == [
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/drive",
    ]


def test_get_scopes_expands_short_names(monkeypatch):
    monkeypatch.setenv("GOOGLE_SLIDES_SCOPES", "presentations, drive ,")
    assert auth.get_scopes() == [
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/drive",
    ]


def test_get_scopes_preserves_full_urls(monkeypatch):
    monkeypatch.setenv(
        "GOOGLE_SLIDES_SCOPES",
        "https://www.googleapis.com/auth/drive.file, drive.readonly",
    )
    assert auth.get_scopes() == [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
    ]


def test_load_credentials_missing_token_non_interactive(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "nope.json"))
    with pytest.raises(auth.AuthError) as exc:
        auth.load_credentials()
    assert "google-slides-mcp-auth" in str(exc.value)


def test_ensure_credentials_no_browser_returns_none(monkeypatch, tmp_path):
    # No token + browser disabled -> swallow the AuthError, return None, log only.
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "nope.json"))
    monkeypatch.setenv("GOOGLE_SLIDES_NO_BROWSER_AUTH", "1")
    assert auth.ensure_credentials() is None
