"""OAuth2 (installed-app) authentication for the Google Slides MCP.

A stdio MCP server is spawned fresh by the client and cannot reliably run an
interactive browser consent flow on its first tool call. To avoid that, auth is
split in two:

* ``auth_main()`` is a separate console command (``google-slides-mcp-auth``) that
  the user runs **once** interactively. It opens a browser, performs the consent
  flow, and writes a cached token to ``GOOGLE_TOKEN_PATH``.
* ``load_credentials()`` is what the server uses at runtime. It loads the cached
  token, refreshes it silently when possible, and raises an actionable error if
  the user has not logged in yet.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Full scopes: presentations (read/write Slides) + drive (so files.copy can clone
# an arbitrary template the user owns by ID, not just files this app created).
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive",
]

DEFAULT_TOKEN_PATH = "~/.config/google-slides-mcp/token.json"
DEFAULT_CLIENT_SECRET = "client_secret.json"


def get_scopes() -> list[str]:
    """Return the OAuth scopes, allowing an env override."""
    raw = os.environ.get("GOOGLE_SLIDES_SCOPES")
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SCOPES)


def _token_path() -> Path:
    return Path(os.environ.get("GOOGLE_TOKEN_PATH", DEFAULT_TOKEN_PATH)).expanduser()


def _client_secret_path() -> Path:
    return Path(
        os.environ.get("GOOGLE_CLIENT_SECRET", DEFAULT_CLIENT_SECRET)
    ).expanduser()


class AuthError(RuntimeError):
    """Raised when usable credentials cannot be obtained without user action."""


def _write_token(creds: Credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    # Token file contains a refresh token; restrict to the owner.
    try:
        path.chmod(0o600)
    except OSError:
        # Best effort (e.g. on filesystems without POSIX perms).
        pass


def load_credentials() -> Credentials:
    """Load cached credentials for the server, refreshing if needed.

    Raises:
        AuthError: if no token exists yet, or it cannot be refreshed. The message
            tells the user to run the ``google-slides-mcp-auth`` command.
    """
    token_path = _token_path()
    scopes = get_scopes()

    if not token_path.exists():
        raise AuthError(
            f"No cached Google credentials at {token_path}. Run the one-time login "
            "first:\n\n    GOOGLE_CLIENT_SECRET=/path/to/client_secret.json "
            "google-slides-mcp-auth\n\n"
            "(or set GOOGLE_TOKEN_PATH / GOOGLE_CLIENT_SECRET to match your config)."
        )

    creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - surface a friendly message
            raise AuthError(
                f"Cached token at {token_path} could not be refreshed ({exc}). "
                "Re-run `google-slides-mcp-auth` to sign in again."
            ) from exc
        _write_token(creds, token_path)
        return creds

    raise AuthError(
        f"Cached token at {token_path} is invalid and has no refresh token. "
        "Re-run `google-slides-mcp-auth` to sign in again."
    )


def run_login_flow() -> Path:
    """Run the interactive browser consent flow and cache the token.

    Returns:
        The path the token was written to.
    """
    client_secret = _client_secret_path()
    if not client_secret.exists():
        raise AuthError(
            f"OAuth client secret not found at {client_secret}. Download a Desktop "
            "OAuth client from Google Cloud Console (APIs & Services -> Credentials) "
            "and set GOOGLE_CLIENT_SECRET to its path."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret), get_scopes()
    )
    # port=0 lets the OS pick a free port for the loopback redirect.
    creds = flow.run_local_server(port=0)

    token_path = _token_path()
    _write_token(creds, token_path)
    return token_path


def auth_main() -> None:
    """Console entry point: ``google-slides-mcp-auth``."""
    try:
        token_path = run_login_flow()
    except AuthError as exc:
        raise SystemExit(f"Authentication failed: {exc}")
    print(f"Success. Token cached at {token_path}.")
    print("You can now configure the MCP server (google-slides-mcp).")


if __name__ == "__main__":
    auth_main()
