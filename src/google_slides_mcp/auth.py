"""OAuth2 (installed-app) authentication for the Google Slides MCP.

Auth uses a standard interactive OAuth 2.0 installed-app model designed to roll
out to multiple people: distribute one **Desktop** OAuth client JSON to your
users, and each person logs in once in a browser, producing a personal refresh
token cached on their machine. There is no shared, single-user token.

Two paths obtain credentials:

* ``run_login_flow()`` / the ``google-slides-mcp-auth`` console command run the
  interactive browser consent flow explicitly and cache the token. Recommended as
  a one-time onboarding step, and required for headless setups.
* ``ensure_credentials()`` is called at server startup. If a valid (or
  refreshable) token already exists it returns immediately with **no** browser;
  otherwise it runs the consent flow automatically so the very first launch "just
  works". Its stdout is redirected to stderr so the interactive flow can never
  corrupt the MCP stdio JSON-RPC channel.

Environment variables (the ``GOOGLE_*`` names and the shorter aliases are
interchangeable; the ``GOOGLE_*`` name wins if both are set):

* OAuth client secret JSON: ``GOOGLE_CLIENT_SECRET`` or ``CREDENTIALS_PATH``.
* Token cache path: ``GOOGLE_TOKEN_PATH`` or ``TOKEN_PATH``.
* Scope override (advanced): ``GOOGLE_SLIDES_SCOPES``.
* Disable the automatic browser flow at startup: ``GOOGLE_SLIDES_NO_BROWSER_AUTH``.
"""

from __future__ import annotations

import contextlib
import os
import sys
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

_TRUTHY = {"1", "true", "yes", "on"}

# Google OAuth scopes must be fully-qualified URLs; bare names like "drive" are
# rejected with `invalid_scope`. Short names are expanded under this prefix.
_SCOPE_PREFIX = "https://www.googleapis.com/auth/"


def _normalize_scope(scope: str) -> str:
    """Expand a short scope name (e.g. ``drive``) to its full Google URL."""
    if "://" in scope:
        return scope
    return _SCOPE_PREFIX + scope.lstrip("/")


def get_scopes() -> list[str]:
    """Return the OAuth scopes, allowing an env override.

    ``GOOGLE_SLIDES_SCOPES`` may use either full URLs or short names
    (``presentations,drive``); short names are expanded to the required
    ``https://www.googleapis.com/auth/...`` form so Google doesn't reject them.
    """
    raw = os.environ.get("GOOGLE_SLIDES_SCOPES")
    if raw:
        return [_normalize_scope(s.strip()) for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SCOPES)


def _first_env(*names: str) -> str | None:
    """Return the first non-empty value among the given env var names."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _token_path() -> Path:
    raw = _first_env("GOOGLE_TOKEN_PATH", "TOKEN_PATH") or DEFAULT_TOKEN_PATH
    return Path(raw).expanduser()


def _client_secret_path() -> Path:
    raw = _first_env("GOOGLE_CLIENT_SECRET", "CREDENTIALS_PATH") or DEFAULT_CLIENT_SECRET
    return Path(raw).expanduser()


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


def _run_consent_flow() -> Credentials:
    """Run the interactive browser consent flow and cache the resulting token."""
    client_secret = _client_secret_path()
    if not client_secret.exists():
        raise AuthError(
            f"OAuth client secret not found at {client_secret}. Download a Desktop "
            "OAuth client from Google Cloud Console (APIs & Services -> Credentials) "
            "and point GOOGLE_CLIENT_SECRET (or CREDENTIALS_PATH) at its path."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), get_scopes())
    # port=0 lets the OS pick a free port for the loopback redirect.
    creds = flow.run_local_server(port=0)

    _write_token(creds, _token_path())
    return creds


def load_credentials(*, allow_interactive: bool = False) -> Credentials:
    """Load credentials for the server, refreshing (or re-authing) as needed.

    Args:
        allow_interactive: When True, fall back to the interactive browser consent
            flow if there is no usable cached token. When False (the default, used
            on every tool call) a missing/expired token raises :class:`AuthError`
            instead of unexpectedly opening a browser mid-session.

    Raises:
        AuthError: if usable credentials can't be obtained without user action and
            ``allow_interactive`` is False.
    """
    token_path = _token_path()
    scopes = get_scopes()

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - surface a friendly message
            if not allow_interactive:
                raise AuthError(
                    f"Cached token at {token_path} could not be refreshed ({exc}). "
                    "Re-run `google-slides-mcp-auth` to sign in again."
                ) from exc
            # Fall through to a fresh interactive consent below.
        else:
            _write_token(creds, token_path)
            return creds

    if allow_interactive:
        return _run_consent_flow()

    if token_path.exists():
        raise AuthError(
            f"Cached token at {token_path} is invalid and has no refresh token. "
            "Re-run `google-slides-mcp-auth` to sign in again."
        )
    raise AuthError(
        f"No cached Google credentials at {token_path}. Run the one-time login "
        "first:\n\n    GOOGLE_CLIENT_SECRET=/path/to/client_secret.json "
        "google-slides-mcp-auth\n\n"
        "(or set GOOGLE_TOKEN_PATH / GOOGLE_CLIENT_SECRET — equivalently "
        "TOKEN_PATH / CREDENTIALS_PATH — to match your config)."
    )


def ensure_credentials() -> Credentials | None:
    """Resolve credentials at server startup.

    Fast path: a valid or refreshable cached token returns immediately with **no**
    browser. Otherwise, unless ``GOOGLE_SLIDES_NO_BROWSER_AUTH`` is set, run the
    interactive consent flow so a user's first launch works without a separate
    command. The flow's stdout is redirected to stderr so it can never corrupt the
    stdio JSON-RPC channel.

    Never raises: on failure it logs to stderr and returns ``None`` so the server
    still starts and individual tool calls surface a clear :class:`AuthError`.
    """
    no_browser = (os.environ.get("GOOGLE_SLIDES_NO_BROWSER_AUTH") or "").strip().lower()
    allow_interactive = no_browser not in _TRUTHY
    try:
        # redirect_stdout guards the MCP stdio channel during any consent prints;
        # the fast (already-authed) path prints nothing.
        with contextlib.redirect_stdout(sys.stderr):
            return load_credentials(allow_interactive=allow_interactive)
    except AuthError as exc:
        print(f"google-slides-mcp: {exc}", file=sys.stderr)
        return None


def run_login_flow() -> Path:
    """Run the interactive browser consent flow and cache the token.

    Returns:
        The path the token was written to.
    """
    _run_consent_flow()
    return _token_path()


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
