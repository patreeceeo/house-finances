"""Who is using the app and what they may do (no Streamlit).

Hosted, the app sits behind Cloudflare Access, which signs people in and
adds a signed token (the ``Cf-Access-Jwt-Assertion`` header) to every
request it lets through. The app verifies that token and takes the email
from it; the email decides the role:

* **editor** - can do everything: the emails in ``HOUSEFIN_EDITORS``
  (comma-separated, any case).
* **viewer** - read-only: anyone else Access lets in.

Hosted mode is on when ``HOUSEFIN_ACCESS_TEAM_DOMAIN`` (e.g.
``myhouse.cloudflareaccess.com``) and ``HOUSEFIN_ACCESS_AUD`` (the Access
application's AUD tag) are set; a request without a valid token is then
refused. Otherwise the app runs locally with no sign-in, as
``HOUSEFIN_ROLE`` (default editor).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

EDITOR = "editor"
VIEWER = "viewer"
ROLES = (EDITOR, VIEWER)

TOKEN_HEADER = "Cf-Access-Jwt-Assertion"


class AuthError(Exception):
    """The request can't be tied to a signed-in person."""


@dataclass(frozen=True)
class User:
    email: str | None  # None when running locally without sign-in
    role: str

    @property
    def can_edit(self) -> bool:
        return self.role == EDITOR


@dataclass(frozen=True)
class AccessConfig:
    team_domain: str
    audience: str

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"


def access_config(env: Mapping[str, str] | None = None) -> AccessConfig | None:
    """Hosted-mode settings from the environment, or None for local use.
    Setting only one of the two is a mistake, not local use."""
    env = os.environ if env is None else env
    team = env.get("HOUSEFIN_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
    aud = env.get("HOUSEFIN_ACCESS_AUD", "").strip()
    if not team and not aud:
        return None
    if not team or not aud:
        raise AuthError(
            "Set both HOUSEFIN_ACCESS_TEAM_DOMAIN and HOUSEFIN_ACCESS_AUD, or neither."
        )
    return AccessConfig(team, aud)


def editors(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The editor emails in ``HOUSEFIN_EDITORS``, casefolded."""
    env = os.environ if env is None else env
    return frozenset(
        e.strip().casefold() for e in env.get("HOUSEFIN_EDITORS", "").split(",") if e.strip()
    )


def role_for(email: str, editor_emails: frozenset[str]) -> str:
    """Editor if listed, else viewer: someone left off the list can only look."""
    return EDITOR if email.strip().casefold() in editor_emails else VIEWER


def local_role(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    role = env.get("HOUSEFIN_ROLE", EDITOR).strip().lower() or EDITOR
    if role not in ROLES:
        raise AuthError(f"HOUSEFIN_ROLE must be {' or '.join(ROLES)}, not {role!r}.")
    return role


def verify_token(token: str, config: AccessConfig, signing_key: Callable[[str], object]) -> str:
    """The email in a Cloudflare Access token, after checking its signature,
    audience, issuer and expiry. ``signing_key(token)`` returns the public key
    the token was signed with (:func:`jwks_signing_key` fetches Cloudflare's)."""
    import jwt

    try:
        claims = jwt.decode(
            token,
            signing_key(token),
            algorithms=["RS256"],
            audience=config.audience,
            issuer=config.issuer,
            options={"require": ["exp", "aud", "iss"]},
        )
    except jwt.PyJWTError as e:
        raise AuthError(f"The sign-in token is not valid ({e}).") from e
    email = claims.get("email")
    if not email:
        raise AuthError("The sign-in token has no email.")
    return email


def jwks_signing_key(config: AccessConfig) -> Callable[[str], object]:
    """Looks up a token's key in the team's published certs (fetched once,
    then cached by PyJWT and refreshed when an unknown key id shows up)."""
    import jwt

    client = jwt.PyJWKClient(config.certs_url)
    return lambda token: client.get_signing_key_from_jwt(token).key


def current_user(
    headers: Mapping[str, str],
    *,
    env: Mapping[str, str] | None = None,
    signing_key: Callable[[str], object] | None = None,
) -> User:
    """The person behind a request (its HTTP headers) and their role.

    Locally: no email, role from ``HOUSEFIN_ROLE``. Hosted: the verified
    token's email; raises AuthError without a valid token.
    """
    config = access_config(env)
    if config is None:
        return User(None, local_role(env))
    lowered = {k.lower(): v for k, v in headers.items()}
    token = lowered.get(TOKEN_HEADER.lower())
    if not token:
        raise AuthError("No sign-in token: open the app through its Cloudflare Access address.")
    email = verify_token(token, config, signing_key or jwks_signing_key(config))
    return User(email, role_for(email, editors(env)))
