"""Run KU Portal MCP over authenticated Streamable HTTP.

This stays separate from the upstream stdio entrypoint so upstream updates are
easy to consume. It implements OAuth authorization-code + PKCE for one owner
and persists only hashed grants and tokens in SQLite.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)


def _load_docker_secret(name: str, path: str) -> None:
    if name not in os.environ:
        secret_path = Path(path)
        if secret_path.is_file():
            os.environ[name] = secret_path.read_text().strip()


_load_docker_secret("KU_PORTAL_ID", "/run/secrets/ku_portal_id")
_load_docker_secret("KU_PORTAL_PW", "/run/secrets/ku_portal_pw")
_load_docker_secret(
    "MCP_OWNER_APPROVAL_SECRET", "/run/secrets/mcp_owner_approval_secret"
)
_load_docker_secret("MCP_OAUTH_FERNET_KEY", "/run/secrets/mcp_oauth_fernet_key")

from ku_portal_mcp.server import server  # noqa: E402


os.umask(0o077)
PUBLIC_BASE = os.environ["MCP_PUBLIC_BASE_URL"].rstrip("/")
RESOURCE_URL = f"{PUBLIC_BASE}/mcp"
OWNER_IPS = {
    str(ipaddress.ip_address(value.strip()))
    for value in os.environ["MCP_OWNER_IPS"].split(",")
    if value.strip()
}
DB_PATH = Path(os.environ.get("MCP_OAUTH_DB", "/home/app/.cache/ku-portal-mcp/oauth.sqlite3"))
SCOPE = "ku:read"
PUBLIC_URL = urlparse(PUBLIC_BASE)
PUBLIC_ORIGIN = f"{PUBLIC_URL.scheme}://{PUBLIC_URL.netloc}"
CONSENT_COOKIE_PATH = f"{PUBLIC_URL.path.rstrip('/')}/consent" or "/consent"
CSRF_COOKIE_NAME = "ku_mcp_consent_csrf"
CLIENT_IP_HEADER = os.environ.get("MCP_CLIENT_IP_HEADER", "x-forwarded-for").lower()
if CLIENT_IP_HEADER not in {"x-forwarded-for", "x-real-ip"}:
    raise ValueError("MCP_CLIENT_IP_HEADER must be x-forwarded-for or x-real-ip")
TRUSTED_PROXY_NETWORKS = tuple(
    ipaddress.ip_network(value.strip(), strict=False)
    for value in os.environ.get("MCP_TRUSTED_PROXY_IPS", "").split(",")
    if value.strip()
)


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


MAX_REGISTERED_CLIENTS = _positive_int_env("MCP_MAX_REGISTERED_CLIENTS", 100)
CLIENT_RETENTION_SECONDS = _positive_int_env(
    "MCP_CLIENT_RETENTION_SECONDS", 90 * 24 * 60 * 60
)
MAX_CLIENT_PAYLOAD_BYTES = _positive_int_env("MCP_MAX_CLIENT_PAYLOAD_BYTES", 16 * 1024)
MAX_PENDING_AUTHORIZATIONS = _positive_int_env("MCP_MAX_PENDING_AUTHORIZATIONS", 50)
MAX_PENDING_PER_CLIENT = _positive_int_env("MCP_MAX_PENDING_PER_CLIENT", 5)
PRUNE_INTERVAL_SECONDS = _positive_int_env("MCP_PRUNE_INTERVAL_SECONDS", 60)
REGISTRATION_OPEN_UNTIL = int(os.environ.get("MCP_REGISTRATION_OPEN_UNTIL", "0"))
if REGISTRATION_OPEN_UNTIL < 0:
    raise ValueError("MCP_REGISTRATION_OPEN_UNTIL must be zero or a Unix timestamp")
OWNER_APPROVAL_SECRET = os.environ.get("MCP_OWNER_APPROVAL_SECRET", "")
if len(OWNER_APPROVAL_SECRET) < 32:
    raise ValueError("MCP_OWNER_APPROVAL_SECRET must contain at least 32 characters")
try:
    raw_fernet_keys = os.environ.get("MCP_OAUTH_FERNET_KEYS") or os.environ[
        "MCP_OAUTH_FERNET_KEY"
    ]
    fernet_keys = [
        Fernet(value.strip().encode())
        for value in raw_fernet_keys.replace("\n", ",").split(",")
        if value.strip()
    ]
    if not fernet_keys:
        raise ValueError("no Fernet keys configured")
    CLIENT_PAYLOAD_CIPHER = MultiFernet(fernet_keys)
except (KeyError, ValueError) as exc:
    raise ValueError("MCP_OAUTH_FERNET_KEY(S) must contain valid Fernet keys") from exc


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _secret_text(value: object) -> str:
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value or "")


class OwnerOAuthProvider:
    """Persistent OAuth provider for this single-owner private MCP server."""

    def __init__(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._next_prune_at = 0.0
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                created_at INTEGER NOT NULL DEFAULT 0,
                last_used_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pending (
                token_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at INTEGER NOT NULL,
                client_id TEXT, attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS codes (
                code_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access_tokens (
                token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, scopes TEXT NOT NULL,
                resource TEXT, subject TEXT NOT NULL, expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, scopes TEXT NOT NULL,
                resource TEXT, subject TEXT NOT NULL, expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS health_probe (
                id INTEGER PRIMARY KEY CHECK (id = 1), checked_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            """
        )

        def ensure_column(table: str, column: str, declaration: str) -> None:
            columns = {
                row[1]
                for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in columns:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                )

        ensure_column("clients", "created_at", "INTEGER NOT NULL DEFAULT 0")
        ensure_column("clients", "last_used_at", "INTEGER NOT NULL DEFAULT 0")
        ensure_column("pending", "client_id", "TEXT")
        ensure_column("pending", "attempts", "INTEGER NOT NULL DEFAULT 0")
        for table in ("access_tokens", "refresh_tokens"):
            ensure_column(table, "resource", "TEXT")
            ensure_column(table, "subject", "TEXT NOT NULL DEFAULT 'owner'")
            ensure_column(table, "expires_at", "INTEGER NOT NULL DEFAULT 0")
        for token_hash, payload in self.db.execute(
            "SELECT token_hash, payload FROM pending WHERE client_id IS NULL"
        ).fetchall():
            try:
                client_id = json.loads(payload).get("client_id")
            except (json.JSONDecodeError, AttributeError):
                client_id = None
            if client_id:
                self.db.execute(
                    "UPDATE pending SET client_id = ? WHERE token_hash = ?",
                    (client_id, token_hash),
                )
        now = int(time.time())
        self.db.execute(
            "UPDATE clients SET created_at = ?, last_used_at = ? "
            "WHERE created_at = 0 OR last_used_at = 0",
            (now, now),
        )
        self._migrate_client_payload_encryption()
        self.db.commit()
        self._prune_expired(force=True)

    @staticmethod
    def _encrypt_client_payload(payload: str) -> str:
        return "fernet:" + CLIENT_PAYLOAD_CIPHER.encrypt(payload.encode()).decode()

    @staticmethod
    def _decrypt_client_payload(payload: str) -> str | None:
        if not payload.startswith("fernet:"):
            return payload
        try:
            return CLIENT_PAYLOAD_CIPHER.decrypt(payload.removeprefix("fernet:").encode()).decode()
        except InvalidToken:
            return None

    def _migrate_client_payload_encryption(self) -> None:
        rows = self.db.execute("SELECT client_id, payload FROM clients").fetchall()
        for client_id, payload in rows:
            cleartext = self._decrypt_client_payload(payload)
            if cleartext is None:
                continue
            self.db.execute(
                "UPDATE clients SET payload = ? WHERE client_id = ?",
                (self._encrypt_client_payload(cleartext), client_id),
            )

    def _prune_expired(self, *, force: bool = False) -> None:
        monotonic_now = time.monotonic()
        if not force and monotonic_now < self._next_prune_at:
            return
        self._next_prune_at = monotonic_now + PRUNE_INTERVAL_SECONDS
        now = int(time.time())
        with self._lock:
            for table in ("pending", "codes", "access_tokens", "refresh_tokens"):
                self.db.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))
            inactive_before = now - CLIENT_RETENTION_SECONDS
            self.db.execute(
                """
                DELETE FROM clients
                WHERE last_used_at < ?
                  AND client_id NOT IN (
                      SELECT client_id FROM access_tokens
                      UNION SELECT client_id FROM refresh_tokens
                  )
                """,
                (inactive_before,),
            )
            self.db.commit()

    def healthcheck(self) -> bool:
        try:
            with self._lock:
                self.db.execute(
                    "INSERT OR REPLACE INTO health_probe(id, checked_at) VALUES (1, ?)",
                    (int(time.time()),),
                )
                self.db.commit()
            return True
        except sqlite3.Error:
            return False

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        self._prune_expired()
        with self._lock:
            row = self.db.execute(
                "SELECT payload FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE clients SET last_used_at = ? WHERE client_id = ?",
                (int(time.time()), client_id),
            )
            self.db.commit()
            payload = self._decrypt_client_payload(row[0])
            if payload is None:
                return None
        return OAuthClientInformationFull.model_validate_json(payload)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._prune_expired()
        serialized = client_info.model_dump_json()
        if len(serialized.encode()) > MAX_CLIENT_PAYLOAD_BYTES:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="OAuth client metadata is too large",
            )
        uris = [urlparse(str(uri)) for uri in client_info.redirect_uris or []]
        allowed = all(
            uri.scheme == "https"
            and uri.hostname == "chatgpt.com"
            and (uri.path == "/connector_platform_oauth_redirect" or uri.path.startswith("/connector/oauth/"))
            for uri in uris
        )
        if not uris or len(uris) > 5 or not allowed or any(len(uri.geturl()) > 1024 for uri in uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Only official ChatGPT OAuth redirect URIs are allowed",
            )
        with self._lock:
            existing = self.db.execute(
                "SELECT payload FROM clients WHERE client_id = ?", (client_info.client_id,)
            ).fetchone()
            if existing:
                stored_payload = self._decrypt_client_payload(existing[0])
                if stored_payload is None:
                    raise RegistrationError(
                        error="invalid_client_metadata",
                        error_description="Existing OAuth client metadata cannot be decrypted",
                    )
                stored = OAuthClientInformationFull.model_validate_json(stored_payload)
                if not secrets.compare_digest(
                    _secret_text(stored.client_secret),
                    _secret_text(client_info.client_secret),
                ):
                    raise RegistrationError(
                        error="invalid_client_metadata",
                        error_description="Existing OAuth client metadata cannot be replaced",
                    )
            else:
                consumed_row = self.db.execute(
                    "SELECT value FROM oauth_state WHERE key = 'registration_window_consumed'"
                ).fetchone()
                consumed_window = int(consumed_row[0]) if consumed_row else 0
                now = int(time.time())
                registration_open = (
                    now <= REGISTRATION_OPEN_UNTIL
                    and consumed_window != REGISTRATION_OPEN_UNTIL
                )
                if not registration_open:
                    raise RegistrationError(
                        error="unapproved_software_statement",
                        error_description="New OAuth client registration is closed",
                    )
            registered_count = self.db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            if not existing and registered_count >= MAX_REGISTERED_CLIENTS:
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="OAuth client registration capacity reached",
                )
            now = int(time.time())
            self.db.execute(
                """
                INSERT INTO clients(client_id, payload, created_at, last_used_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    payload = excluded.payload,
                    last_used_at = excluded.last_used_at
                """,
                (
                    client_info.client_id,
                    self._encrypt_client_payload(serialized),
                    now,
                    now,
                ),
            )
            if not existing:
                self.db.execute(
                    """
                    INSERT INTO oauth_state(key, value)
                    VALUES ('registration_window_consumed', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(REGISTRATION_OPEN_UNTIL),),
                )
            self.db.commit()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._prune_expired()
        token = secrets.token_urlsafe(32)
        payload = {
            "client_id": client.client_id,
            "state": params.state,
            "scopes": params.scopes or [SCOPE],
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource or RESOURCE_URL,
        }
        with self._lock:
            pending_count = self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            client_pending_count = self.db.execute(
                "SELECT COUNT(*) FROM pending WHERE client_id = ?", (client.client_id,)
            ).fetchone()[0]
            if pending_count >= MAX_PENDING_AUTHORIZATIONS or client_pending_count >= MAX_PENDING_PER_CLIENT:
                raise AuthorizeError(
                    error="temporarily_unavailable",
                    error_description="Too many pending authorization requests",
                )
            self.db.execute(
                "INSERT INTO pending(token_hash, payload, expires_at, client_id) VALUES (?, ?, ?, ?)",
                (
                    _digest(token),
                    json.dumps(payload),
                    int(time.time()) + 300,
                    client.client_id,
                ),
            )
            self.db.commit()
        return f"{PUBLIC_BASE}/consent?{urlencode({'request': token})}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._prune_expired()
        with self._lock:
            row = self.db.execute(
                "SELECT payload, expires_at FROM codes WHERE code_hash = ?",
                (_digest(authorization_code),),
            ).fetchone()
        if not row or row[1] < time.time():
            return None
        payload = json.loads(row[0])
        if payload["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **payload)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            cursor = self.db.execute(
                "DELETE FROM codes WHERE code_hash = ?",
                (_digest(authorization_code.code),),
            )
            if cursor.rowcount != 1:
                self.db.rollback()
                raise TokenError(
                    error="invalid_grant",
                    error_description="Authorization code is invalid or was already used",
                )
            return self._issue_tokens(
                client.client_id,
                authorization_code.scopes,
                authorization_code.resource or RESOURCE_URL,
            )

    def _issue_tokens(self, client_id: str, scopes: list[str], resource: str) -> OAuthToken:
        now = int(time.time())
        access = secrets.token_urlsafe(40)
        refresh = secrets.token_urlsafe(48)
        scope_text = " ".join(scopes)
        subject = "owner"
        with self._lock:
            self.db.execute(
                """
                INSERT INTO access_tokens(
                    token_hash, client_id, scopes, resource, subject, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_digest(access), client_id, scope_text, resource, subject, now + 3600),
            )
            self.db.execute(
                """
                INSERT INTO refresh_tokens(
                    token_hash, client_id, scopes, resource, subject, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_digest(refresh), client_id, scope_text, resource, subject, now + 7776000),
            )
            self.db.commit()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            scope=scope_text,
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        self._prune_expired()
        with self._lock:
            row = self.db.execute(
                "SELECT client_id, scopes, resource, subject, expires_at "
                "FROM refresh_tokens WHERE token_hash = ?",
                (_digest(refresh_token),),
            ).fetchone()
        if not row or row[0] != client.client_id or row[4] < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row[0],
            scopes=row[1].split(),
            resource=row[2],
            subject=row[3],
            expires_at=row[4],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        with self._lock:
            deleted = self.db.execute(
                "DELETE FROM refresh_tokens WHERE token_hash = ? RETURNING resource",
                (_digest(refresh_token.token),),
            ).fetchone()
            if deleted is None:
                self.db.execute(
                    "DELETE FROM access_tokens WHERE client_id = ?", (client.client_id,)
                )
                self.db.execute(
                    "DELETE FROM refresh_tokens WHERE client_id = ?", (client.client_id,)
                )
                self.db.commit()
                raise TokenError(
                    error="invalid_grant",
                    error_description="Refresh token is invalid or was already used",
                )
            requested = scopes or refresh_token.scopes
            if not set(requested).issubset(set(refresh_token.scopes)):
                requested = refresh_token.scopes
            return self._issue_tokens(
                client.client_id, requested, deleted[0] or RESOURCE_URL
            )

    async def load_access_token(self, token: str) -> AccessToken | None:
        self._prune_expired()
        with self._lock:
            row = self.db.execute(
                "SELECT client_id, scopes, resource, subject, expires_at "
                "FROM access_tokens WHERE token_hash = ?",
                (_digest(token),),
            ).fetchone()
        if not row or row[4] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=row[0],
            scopes=row[1].split(),
            resource=row[2],
            subject=row[3],
            expires_at=row[4],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        token_hash = _digest(token.token)
        with self._lock:
            self.db.execute("DELETE FROM access_tokens WHERE token_hash = ?", (token_hash,))
            self.db.execute("DELETE FROM refresh_tokens WHERE token_hash = ?", (token_hash,))
            self.db.commit()

    def prepare_consent(self, request_token: str, csrf_token: str) -> bool:
        self._prune_expired()
        with self._lock:
            row = self.db.execute(
                "SELECT payload, expires_at FROM pending WHERE token_hash = ?",
                (_digest(request_token),),
            ).fetchone()
            if not row or row[1] < time.time():
                return False
            payload = json.loads(row[0])
            expected_csrf_hash = payload.get("csrf_hash")
            if expected_csrf_hash:
                return secrets.compare_digest(expected_csrf_hash, _digest(csrf_token))
            payload["csrf_hash"] = _digest(csrf_token)
            self.db.execute(
                "UPDATE pending SET payload = ? WHERE token_hash = ?",
                (json.dumps(payload), _digest(request_token)),
            )
            self.db.commit()
            return True

    def record_failed_owner_approval(self, request_token: str) -> None:
        """Invalidate a pending approval after three wrong owner secrets."""
        with self._lock:
            token_hash = _digest(request_token)
            cursor = self.db.execute(
                "UPDATE pending SET attempts = attempts + 1 WHERE token_hash = ?",
                (token_hash,),
            )
            if cursor.rowcount == 1:
                self.db.execute(
                    "DELETE FROM pending WHERE token_hash = ? AND attempts >= 3",
                    (token_hash,),
                )
            self.db.commit()

    def approve(self, request_token: str, csrf_token: str) -> str | None:
        self._prune_expired()
        with self._lock:
            row = self.db.execute(
                "SELECT payload, expires_at FROM pending WHERE token_hash = ?",
                (_digest(request_token),),
            ).fetchone()
            if not row or row[1] < time.time():
                return None
            payload = json.loads(row[0])
            expected_csrf_hash = payload.get("csrf_hash", "")
            if not expected_csrf_hash or not secrets.compare_digest(
                expected_csrf_hash, _digest(csrf_token)
            ):
                return None
            self.db.execute(
                "DELETE FROM pending WHERE token_hash = ?", (_digest(request_token),)
            )
            code = secrets.token_urlsafe(32)
            code_payload = {
                "scopes": payload["scopes"],
                "expires_at": time.time() + 300,
                "client_id": payload["client_id"],
                "code_challenge": payload["code_challenge"],
                "redirect_uri": payload["redirect_uri"],
                "redirect_uri_provided_explicitly": payload[
                    "redirect_uri_provided_explicitly"
                ],
                "resource": payload["resource"],
                "subject": "owner",
            }
            self.db.execute(
                "INSERT INTO codes(code_hash, payload, expires_at) VALUES (?, ?, ?)",
                (_digest(code), json.dumps(code_payload), int(time.time()) + 300),
            )
            self.db.commit()
        query = {"code": code}
        if payload.get("state"):
            query["state"] = payload["state"]
        separator = "&" if "?" in payload["redirect_uri"] else "?"
        return f"{payload['redirect_uri']}{separator}{urlencode(query)}"


provider = OwnerOAuthProvider()


def _request_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in network for network in TRUSTED_PROXY_NETWORKS):
        return str(peer_ip)
    forwarded = request.headers.get(CLIENT_IP_HEADER, "")
    if CLIENT_IP_HEADER == "x-forwarded-for":
        hops = []
        for raw_hop in forwarded.split(","):
            try:
                hops.append(ipaddress.ip_address(raw_hop.strip()))
            except ValueError:
                return str(peer_ip)
        for hop in reversed(hops):
            if not any(hop in network for network in TRUSTED_PROXY_NETWORKS):
                return str(hop)
        return str(peer_ip)
    try:
        return str(ipaddress.ip_address(forwarded.strip()))
    except ValueError:
        return str(peer_ip)


def _has_valid_consent_origin(request: Request) -> bool:
    origin = request.headers.get("origin", "").rstrip("/")
    if origin:
        return origin == PUBLIC_ORIGIN
    referer = urlparse(request.headers.get("referer", ""))
    return (
        referer.scheme in {"http", "https"}
        and f"{referer.scheme}://{referer.netloc}" == PUBLIC_ORIGIN
    )


def _has_valid_owner_approval_secret(candidate: str) -> bool:
    return secrets.compare_digest(_digest(candidate), _digest(OWNER_APPROVAL_SECRET))


def _secure_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@server.custom_route("/health", methods=["GET"])
async def health(request: Request):
    if provider.healthcheck():
        return _secure_response(JSONResponse({"status": "ok"}))
    return _secure_response(JSONResponse({"status": "error"}, status_code=503))


@server.custom_route("/consent", methods=["GET", "POST"])
async def consent(request: Request):
    request_ip = _request_ip(request)
    if request_ip not in OWNER_IPS:
        return _secure_response(
            PlainTextResponse(
                "OAuth approval is restricted to the owner network.", status_code=403
            )
        )
    token = request.query_params.get("request", "")
    if request.method == "POST":
        if not _has_valid_consent_origin(request):
            return _secure_response(
                PlainTextResponse("Invalid consent origin.", status_code=403)
            )
        form = await request.form()
        token = str(form.get("request", ""))
        csrf_token = str(form.get("csrf_token", ""))
        owner_secret = str(form.get("owner_secret", ""))
        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
        if not csrf_token or not secrets.compare_digest(csrf_token, csrf_cookie):
            return _secure_response(
                PlainTextResponse("Invalid consent CSRF token.", status_code=403)
            )
        if not _has_valid_owner_approval_secret(owner_secret):
            provider.record_failed_owner_approval(token)
            return _secure_response(
                PlainTextResponse("Invalid owner approval secret.", status_code=403)
            )
        redirect = provider.approve(token, csrf_token)
        if not redirect:
            return _secure_response(
                PlainTextResponse(
                    "Authorization request expired or invalid.", status_code=400
                )
            )
        response = RedirectResponse(redirect, status_code=302)
        response.delete_cookie(CSRF_COOKIE_NAME, path=CONSENT_COOKIE_PATH)
        return _secure_response(response)
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)
    if not token or not provider.prepare_consent(token, csrf_token):
        return _secure_response(
            PlainTextResponse(
                "Authorization request expired or invalid.", status_code=400
            )
        )
    safe_token = html.escape(token, quote=True)
    safe_csrf_token = html.escape(csrf_token, quote=True)
    response = HTMLResponse(
        "<!doctype html><meta name='viewport' content='width=device-width'>"
        "<title>KU Portal MCP authorization</title>"
        "<main style='max-width:36rem;margin:10vh auto;font:16px system-ui;padding:2rem'>"
        "<h1>KU Portal MCP 연결 승인</h1>"
        "<p>ChatGPT가 개인 KUPID/LMS 정보를 읽도록 허용합니다. 쓰기 기능은 제공하지 않습니다.</p>"
        f"<form method='post'><input type='hidden' name='request' value='{safe_token}'>"
        f"<input type='hidden' name='csrf_token' value='{safe_csrf_token}'>"
        "<label>소유자 승인 암호 <input type='password' name='owner_secret' "
        "autocomplete='current-password' required></label> "
        "<button style='font-size:1rem;padding:.8rem 1.2rem' type='submit'>연결 승인</button></form>"
        "</main>"
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=300,
        path=CONSENT_COOKIE_PATH,
        secure=PUBLIC_URL.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return _secure_response(response)


def main() -> None:
    server.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
    server.settings.port = int(os.environ.get("MCP_PORT", "8000"))
    server.settings.streamable_http_path = "/mcp"
    public_host = urlparse(PUBLIC_BASE).netloc
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public_host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"https://{public_host}", "http://127.0.0.1:*", "http://localhost:*"],
    )
    server.settings.auth = AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_BASE),
        resource_server_url=AnyHttpUrl(RESOURCE_URL),
        validate_token_resource=True,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[SCOPE],
            default_scopes=[SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    server._auth_server_provider = provider
    from mcp.server.auth.provider import ProviderTokenVerifier

    server._token_verifier = ProviderTokenVerifier(provider)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
