import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from housefin import auth

TEAM = "myhouse.cloudflareaccess.com"
AUD = "aud-tag-123"
HOSTED = {
    "HOUSEFIN_ACCESS_TEAM_DOMAIN": TEAM,
    "HOUSEFIN_ACCESS_AUD": AUD,
    "HOUSEFIN_EDITORS": "Pat@Example.com, sam@example.com",
}


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key, **overrides):
    claims = {
        "email": "pat@example.com",
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "exp": int(time.time()) + 3600,
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


def user(key, tok, env=HOSTED, header=auth.TOKEN_HEADER):
    return auth.current_user({header: tok}, env=env, signing_key=lambda _t: key.public_key())


def test_valid_token_gives_its_email_and_role(key):
    u = user(key, token(key))
    assert u == auth.User("pat@example.com", auth.EDITOR) and u.can_edit
    # Header names are case-insensitive.
    assert user(key, token(key), header="cf-access-jwt-assertion").email == "pat@example.com"


def test_editor_list_is_case_insensitive_and_everyone_else_views(key):
    assert user(key, token(key, email="SAM@example.com")).role == auth.EDITOR
    viewer = user(key, token(key, email="lee@example.com"))
    assert viewer.role == auth.VIEWER and not viewer.can_edit
    # No editors listed: everyone can only look.
    env = {k: v for k, v in HOSTED.items() if k != "HOUSEFIN_EDITORS"}
    assert user(key, token(key), env=env).role == auth.VIEWER


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 10},  # expired
        {"aud": ["someone-else"]},  # another Access application
        {"iss": "https://other.cloudflareaccess.com"},
        {"exp": None},  # never expires
        {"email": None},
    ],
)
def test_bad_tokens_are_refused(key, overrides):
    with pytest.raises(auth.AuthError):
        user(key, token(key, **overrides))


def test_token_signed_by_another_key_is_refused(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(auth.AuthError):
        user(key, token(other))
    with pytest.raises(auth.AuthError):
        user(key, "not-a-token")


def test_hosted_without_token_is_refused(key):
    with pytest.raises(auth.AuthError, match="No sign-in token"):
        auth.current_user({}, env=HOSTED, signing_key=lambda _t: key.public_key())


def test_local_use_takes_the_role_from_the_environment():
    assert auth.current_user({}, env={}) == auth.User(None, auth.EDITOR)
    assert auth.current_user({}, env={"HOUSEFIN_ROLE": "Viewer"}).role == auth.VIEWER
    with pytest.raises(auth.AuthError):
        auth.current_user({}, env={"HOUSEFIN_ROLE": "admin"})


def test_half_configured_access_is_an_error():
    with pytest.raises(auth.AuthError, match="both"):
        auth.access_config({"HOUSEFIN_ACCESS_AUD": AUD})


def test_access_config():
    cfg = auth.access_config({"HOUSEFIN_ACCESS_TEAM_DOMAIN": f"https://{TEAM}/", "HOUSEFIN_ACCESS_AUD": AUD})
    assert cfg.issuer == f"https://{TEAM}"
    assert cfg.certs_url == f"https://{TEAM}/cdn-cgi/access/certs"
