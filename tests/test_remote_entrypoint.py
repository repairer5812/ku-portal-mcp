import asyncio
import importlib
import json
import sys
import time

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthClientInformationFull,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from pydantic import AnyUrl
from starlette.requests import Request


@pytest.fixture(scope="module")
def remote_module(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    oauth_db = tmp_path_factory.mktemp("remote-oauth") / "oauth.sqlite3"
    monkeypatch.setenv(
        "MCP_PUBLIC_BASE_URL", "https://mcp.example.test/ku-mcp"
    )
    monkeypatch.setenv("MCP_OWNER_IPS", "100.64.0.10")
    monkeypatch.setenv("MCP_TRUSTED_PROXY_IPS", "172.20.0.1/32")
    monkeypatch.setenv("MCP_CLIENT_IP_HEADER", "x-forwarded-for")
    monkeypatch.setenv("MCP_OAUTH_DB", str(oauth_db))
    monkeypatch.setenv(
        "MCP_OWNER_APPROVAL_SECRET", "test-owner-secret-1234567890abcdef"
    )
    monkeypatch.setenv("MCP_OAUTH_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MCP_ALLOW_NEW_CLIENT_REGISTRATION", "false")
    sys.modules.pop("remote_entrypoint", None)
    module = importlib.import_module("remote_entrypoint")
    try:
        yield module
    finally:
        module.provider.db.close()
        sys.modules.pop("remote_entrypoint", None)
        monkeypatch.undo()


def _request(client_ip, headers=None):
    encoded_headers = [
        (name.lower().encode(), value.encode())
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/consent",
            "raw_path": b"/consent",
            "query_string": b"",
            "headers": encoded_headers,
            "client": (client_ip, 12345),
            "server": ("mcp.example.test", 443),
        }
    )


def _insert_pending(module, request_token, *, expires_at=None):
    payload = {
        "client_id": "chatgpt-client",
        "state": "state-token",
        "scopes": [module.SCOPE],
        "code_challenge": "challenge",
        "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
        "redirect_uri_provided_explicitly": True,
        "resource": module.RESOURCE_URL,
    }
    module.provider.db.execute(
        "INSERT INTO pending(token_hash, payload, expires_at, client_id) VALUES (?, ?, ?, ?)",
        (
            module._digest(request_token),
            json.dumps(payload),
            expires_at or int(time.time()) + 300,
            payload["client_id"],
        ),
    )
    module.provider.db.commit()


def test_forwarded_ip_is_used_only_for_trusted_proxy(remote_module):
    spoofed = _request(
        "203.0.113.50", {"x-forwarded-for": "100.64.0.10"}
    )
    proxied = _request(
        "172.20.0.1", {"x-forwarded-for": "100.64.0.10, 172.20.0.1"}
    )
    appended_spoof = _request(
        "172.20.0.1", {"x-forwarded-for": "100.64.0.10, 203.0.113.50"}
    )

    assert remote_module._request_ip(spoofed) == "203.0.113.50"
    assert remote_module._request_ip(proxied) == "100.64.0.10"
    assert remote_module._request_ip(appended_spoof) == "203.0.113.50"


def test_consent_origin_must_match_public_origin(remote_module):
    valid = _request(
        "172.20.0.1", {"origin": "https://mcp.example.test"}
    )
    invalid = _request("172.20.0.1", {"origin": "https://evil.example"})
    valid_referer = _request(
        "172.20.0.1",
        {"referer": "https://mcp.example.test/ku-mcp/consent?request=x"},
    )

    assert remote_module._has_valid_consent_origin(valid) is True
    assert remote_module._has_valid_consent_origin(invalid) is False
    assert remote_module._has_valid_consent_origin(valid_referer) is True


def test_consent_csrf_is_bound_to_pending_request(remote_module):
    request_token = "pending-request-token"
    _insert_pending(remote_module, request_token)

    assert remote_module.provider.prepare_consent(request_token, "csrf-token") is True
    assert remote_module.provider.approve(request_token, "wrong-token") is None

    redirect = remote_module.provider.approve(request_token, "csrf-token")
    assert redirect is not None
    assert redirect.startswith(
        "https://chatgpt.com/connector_platform_oauth_redirect?"
    )
    assert "state=state-token" in redirect


def test_expired_oauth_records_are_pruned(remote_module):
    _insert_pending(
        remote_module,
        "expired-request-token",
        expires_at=int(time.time()) - 1,
    )

    remote_module.provider._prune_expired(force=True)

    remaining = remote_module.provider.db.execute(
        "SELECT COUNT(*) FROM pending WHERE token_hash = ?",
        (remote_module._digest("expired-request-token"),),
    ).fetchone()[0]
    assert remaining == 0
    assert remote_module.provider.healthcheck() is True


def _oauth_client(client_id="chatgpt-client", client_secret="client-secret"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uris=[AnyUrl("https://chatgpt.com/connector_platform_oauth_redirect")],
    )


def test_new_client_registration_is_closed_by_default(remote_module):
    with pytest.raises(RegistrationError) as exc_info:
        asyncio.run(remote_module.provider.register_client(_oauth_client("new-client")))
    assert exc_info.value.error_description == "New OAuth client registration is closed"


def test_client_secret_is_encrypted_at_rest(remote_module, monkeypatch):
    monkeypatch.setattr(
        remote_module, "REGISTRATION_OPEN_UNTIL", int(time.time()) + 300
    )
    client = _oauth_client("encrypted-client", "plain-client-secret")
    asyncio.run(remote_module.provider.register_client(client))

    stored = remote_module.provider.db.execute(
        "SELECT payload FROM clients WHERE client_id = ?", ("encrypted-client",)
    ).fetchone()[0]
    assert stored.startswith("fernet:")
    assert "plain-client-secret" not in stored
    loaded = asyncio.run(remote_module.provider.get_client("encrypted-client"))
    assert loaded is not None
    assert loaded.client_secret == "plain-client-secret"


def test_pending_authorizations_are_bounded(remote_module, monkeypatch):
    remote_module.provider.db.execute("DELETE FROM pending")
    remote_module.provider.db.commit()
    monkeypatch.setattr(remote_module, "MAX_PENDING_PER_CLIENT", 1)
    client = _oauth_client("bounded-client")
    params = AuthorizationParams(
        state="state",
        scopes=[remote_module.SCOPE],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://chatgpt.com/connector_platform_oauth_redirect"),
        redirect_uri_provided_explicitly=True,
        resource=remote_module.RESOURCE_URL,
    )

    asyncio.run(remote_module.provider.authorize(client, params))
    with pytest.raises(AuthorizeError) as exc_info:
        asyncio.run(remote_module.provider.authorize(client, params))
    assert exc_info.value.error_description == "Too many pending authorization requests"


def test_owner_approval_requires_second_secret(remote_module):
    assert remote_module._has_valid_owner_approval_secret("wrong") is False
    assert (
        remote_module._has_valid_owner_approval_secret(
            "test-owner-secret-1234567890abcdef"
        )
        is True
    )


def test_wrong_owner_secret_invalidates_pending_after_three_attempts(remote_module):
    request_token = "brute-force-limited-token"
    _insert_pending(remote_module, request_token)

    for _ in range(3):
        remote_module.provider.record_failed_owner_approval(request_token)

    remaining = remote_module.provider.db.execute(
        "SELECT COUNT(*) FROM pending WHERE token_hash = ?",
        (remote_module._digest(request_token),),
    ).fetchone()[0]
    assert remaining == 0


def test_consent_csrf_cannot_be_rebound(remote_module):
    request_token = "csrf-one-time-binding"
    _insert_pending(remote_module, request_token)

    assert remote_module.provider.prepare_consent(request_token, "first-csrf") is True
    assert remote_module.provider.prepare_consent(request_token, "first-csrf") is True
    assert remote_module.provider.prepare_consent(request_token, "replacement-csrf") is False


def test_authorization_code_is_single_use(remote_module):
    client = _oauth_client("single-use-code-client")
    code = AuthorizationCode(
        code="missing-or-used-code",
        scopes=[remote_module.SCOPE],
        expires_at=time.time() + 300,
        client_id=client.client_id,
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://chatgpt.com/connector_platform_oauth_redirect"),
        redirect_uri_provided_explicitly=True,
        resource=remote_module.RESOURCE_URL,
        subject="owner",
    )

    with pytest.raises(TokenError) as exc_info:
        asyncio.run(remote_module.provider.exchange_authorization_code(client, code))
    assert exc_info.value.error == "invalid_grant"


def test_refresh_token_is_single_use(remote_module):
    client = _oauth_client("single-use-refresh-client")
    refresh = RefreshToken(
        token="missing-or-used-refresh-token",
        client_id=client.client_id,
        scopes=[remote_module.SCOPE],
        expires_at=int(time.time()) + 300,
        resource=remote_module.RESOURCE_URL,
        subject="owner",
    )

    with pytest.raises(TokenError) as exc_info:
        asyncio.run(
            remote_module.provider.exchange_refresh_token(client, refresh, [])
        )
    assert exc_info.value.error == "invalid_grant"


def test_new_scope_is_advertised_without_changing_existing_grants(remote_module):
    client = OAuthClientInformationFull(
        client_id="drive-scope-test", redirect_uris=["https://chatgpt.com/connector_platform_oauth_redirect"],
        scope="ku:read",
    )
    remote_module.provider.db.execute(
        "INSERT OR REPLACE INTO clients(client_id,payload,created_at,last_used_at) VALUES (?,?,?,?)",
        (client.client_id, remote_module.provider._encrypt_client_payload(client.model_dump_json()), int(time.time()), int(time.time())),
    )
    remote_module.provider.db.commit()
    loaded = asyncio.run(remote_module.provider.get_client(client.client_id))
    assert set(loaded.scope.split()) == {"ku:read", "ku:drive"}
    stored = remote_module.provider.db.execute("SELECT payload FROM clients WHERE client_id=?", (client.client_id,)).fetchone()[0]
    assert json.loads(remote_module.provider._decrypt_client_payload(stored))["scope"] == "ku:read"


def test_consent_shows_drive_write_permission_only_when_requested(remote_module):
    for scopes in (["ku:read"], ["ku:read", "ku:drive"]):
        token = "consent-scope-" + str(len(scopes))
        _insert_pending(remote_module, token)
        row = remote_module.provider.db.execute("SELECT payload FROM pending WHERE token_hash=?", (remote_module._digest(token),)).fetchone()
        payload = json.loads(row[0])
        payload["scopes"] = scopes
        remote_module.provider.db.execute("UPDATE pending SET payload=? WHERE token_hash=?", (json.dumps(payload), remote_module._digest(token)))
        remote_module.provider.db.commit()
        request = _request("100.64.0.10")
        request.scope["query_string"] = ("request=" + token).encode()
        response = asyncio.run(remote_module.consent(request))
        body = response.body.decode()
        assert response.status_code == 200
        # no-referrer turns Origin into null on native HTML form POSTs.
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["Cache-Control"] == "no-store"
        policy = response.headers["Content-Security-Policy"]
        assert "form-action 'self' https://chatgpt.com/connector_platform_oauth_redirect" in policy
        assert "https://chatgpt.com/connector/oauth/" in policy
        assert "*" not in policy
        assert ("Drive 폴더" in body) == ("ku:drive" in scopes)
        assert ("쓰기 기능은 제공하지 않습니다" in body) == ("ku:drive" not in scopes)


def test_resource_metadata_advertises_optional_drive_scope(remote_module):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient
    path = "/.well-known/oauth-protected-resource/ku-mcp/mcp"
    async def old_metadata(request):
        return JSONResponse({"scopes_supported": ["ku:read"]})
    app = Starlette(routes=[Route(path, old_metadata)])
    remote_module._advertise_optional_drive_scope(app)
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.json()["scopes_supported"] == ["ku:read", "ku:drive"]


@pytest.mark.parametrize("origin", ["null", "https://evil.example"])
def test_opaque_or_foreign_origin_cannot_use_legitimate_referer(remote_module, origin):
    request = _request("100.64.0.10", {
        "origin": origin,
        "referer": "https://mcp.example.test/ku-mcp/consent",
    })
    assert remote_module._has_valid_consent_origin(request) is False


def test_same_origin_native_form_approval_keeps_csrf_and_secret_checks(remote_module):
    from http.cookies import SimpleCookie
    from urllib.parse import urlencode

    token = "native-form-header-regression"
    _insert_pending(remote_module, token)
    get_request = _request("100.64.0.10")
    get_request.scope["query_string"] = ("request=" + token).encode()
    page = asyncio.run(remote_module.consent(get_request))
    assert page.headers["Referrer-Policy"] == "same-origin"
    cookies = SimpleCookie(page.headers["Set-Cookie"])
    csrf = cookies[remote_module.CSRF_COOKIE_NAME].value
    body = urlencode({
        "request": token,
        "csrf_token": csrf,
        "owner_secret": "test-owner-secret-1234567890abcdef",
    }).encode()
    request = _request("100.64.0.10", {
        "origin": "https://mcp.example.test",
        "cookie": f"{remote_module.CSRF_COOKIE_NAME}={csrf}",
        "content-type": "application/x-www-form-urlencoded",
    })
    request.scope["method"] = "POST"
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    response = asyncio.run(remote_module.consent(Request(request.scope, receive)))
    # 303 explicitly returns to ChatGPT as GET, without forwarding the secret.
    assert response.status_code == 303
    assert response.headers["Location"].startswith(
        "https://chatgpt.com/connector_platform_oauth_redirect?"
    )
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_chatgpt_form_redirect_is_not_allowed_on_other_responses(remote_module):
    from starlette.responses import PlainTextResponse
    response = remote_module._secure_response(PlainTextResponse("error", status_code=403))
    assert "form-action 'self';" in response.headers["Content-Security-Policy"]
    assert "chatgpt.com" not in response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
