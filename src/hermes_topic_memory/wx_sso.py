from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Callable, Mapping

_TX_COOKIE = "htm_sso_tx"
_SESSION_COOKIE = "htm_session"
_TX_TTL_SECONDS = 300
_MAX_SESSION_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class WxSsoConfig:
    gateway_base: str
    app_name: str
    app_secret: str
    public_base_url: str
    allowed_openids: tuple[str, ...] = ()
    callback_path: str = "/api/sso/callback"

    @property
    def issuer(self) -> str:
        return self.gateway_base.rstrip("/")

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.callback_path}"

    @property
    def is_https(self) -> bool:
        return self.public_base_url.lower().startswith("https://")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> WxSsoConfig | None:
        env = os.environ if environ is None else environ
        gateway_base = (env.get("WX_GATEWAY_BASE") or "").strip()
        app_name = (env.get("WX_GATEWAY_APP_NAME") or "").strip()
        app_secret = (env.get("WX_GATEWAY_SECRET") or "").strip()
        public_base = (env.get("WX_PUBLIC_BASE_URL") or "").strip()
        if not (gateway_base and app_name and app_secret and public_base):
            return None
        raw_allowed = (env.get("WX_ALLOWED_OPENIDS") or "").strip()
        allowed = tuple(
            item.strip() for item in raw_allowed.split(",") if item.strip()
        )
        return cls(
            gateway_base=gateway_base.rstrip("/"),
            app_name=app_name,
            app_secret=app_secret,
            public_base_url=public_base.rstrip("/"),
            allowed_openids=allowed,
        )


@dataclass(frozen=True, slots=True)
class SsoSession:
    openid: str
    unionid: str | None
    nickname: str | None
    avatar_url: str | None
    issuer: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class _PendingTx:
    tx_id: str
    state: str
    verifier: str
    return_to: str
    expires_at: float


TokenExchanger = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, Any]]]


class WxSsoError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class WxGatewaySso:
    """7-day central SSO client for wx-gateway (Authorization Code + PKCE S256 + HMAC)."""

    def __init__(
        self,
        config: WxSsoConfig,
        *,
        session_secret: str | None = None,
        token_exchanger: TokenExchanger | None = None,
    ) -> None:
        self.config = config
        self._session_key = (session_secret or config.app_secret).encode("utf-8")
        self._exchanger = token_exchanger or _default_token_exchange
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingTx] = {}

    def start(self, *, return_to: str = "/") -> tuple[str, str]:
        """Create a 5-minute PKCE transaction and return (authorize_url, set_cookie_header)."""
        safe_return = _sanitize_return_to(return_to)
        tx_id = secrets.token_urlsafe(24)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = _pkce_s256(verifier)
        now = time.time()
        tx = _PendingTx(
            tx_id=tx_id,
            state=state,
            verifier=verifier,
            return_to=safe_return,
            expires_at=now + _TX_TTL_SECONDS,
        )
        with self._lock:
            self._prune_locked(now)
            self._pending[tx_id] = tx

        query = urllib.parse.urlencode(
            {
                "client_id": self.config.app_name,
                "redirect_uri": self.config.redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        authorize_url = f"{self.config.issuer}/sso/authorize?{query}"
        cookie = _format_cookie(
            _TX_COOKIE,
            tx_id,
            max_age=_TX_TTL_SECONDS,
            secure=self.config.is_https,
        )
        return authorize_url, cookie

    def complete_callback(
        self,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None,
    ) -> tuple[str, tuple[str, ...]]:
        """Validate callback, exchange code via HMAC-signed POST /sso/token, and mint session."""
        if params.get("error"):
            raise WxSsoError("sso_denied", "WeChat SSO authorization was rejected or failed.", 403)
        code = (params.get("code") or "").strip()
        state = (params.get("state") or "").strip()
        iss = (params.get("iss") or "").strip().rstrip("/")
        if not code or not state:
            raise WxSsoError("invalid_callback", "Missing authorization code or state.", 400)
        if iss and iss != self.config.issuer:
            raise WxSsoError("issuer_mismatch", "SSO issuer mismatch.", 400)

        tx_id = _extract_cookie(headers, _TX_COOKIE)
        if not tx_id:
            raise WxSsoError("missing_tx", "Browser SSO session expired. Please retry.", 400)

        now = time.time()
        with self._lock:
            self._prune_locked(now)
            tx = self._pending.pop(tx_id, None)
        if tx is None or tx.expires_at < now:
            raise WxSsoError("expired_tx", "SSO transaction expired. Please retry.", 400)
        if not hmac.compare_digest(tx.state, state):
            raise WxSsoError("state_mismatch", "SSO state mismatch.", 400)

        ts = str(int(time.time() * 1000))
        sig_payload = (
            f"sso-token|{code}|{self.config.redirect_uri}|{tx.verifier}|{state}|{ts}|{self.config.app_name}"
        )
        sig = hmac.new(
            self.config.app_secret.encode("utf-8"),
            sig_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        status, body = self._exchanger(
            f"{self.config.issuer}/sso/token",
            {
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": tx.verifier,
                "state": state,
            },
            {
                "Content-Type": "application/json",
                "X-App-Name": self.config.app_name,
                "X-Ts": ts,
                "X-Internal-Auth": sig,
            },
        )
        if status != 200 or not isinstance(body, dict) or not body.get("ok"):
            raise WxSsoError("token_exchange_failed", "Failed to verify SSO token with gateway.", 502)

        resp_issuer = str(body.get("issuer") or "").rstrip("/")
        if resp_issuer and resp_issuer != self.config.issuer:
            raise WxSsoError("issuer_mismatch", "Token response issuer mismatch.", 400)
        if body.get("app") and body.get("app") != self.config.app_name:
            raise WxSsoError("app_mismatch", "Token response app mismatch.", 400)

        user = body.get("user")
        if not isinstance(user, dict) or not isinstance(user.get("openid"), str) or not user["openid"]:
            raise WxSsoError("invalid_user", "Gateway returned an invalid user identity.", 502)

        openid = user["openid"]
        if self.config.allowed_openids and openid not in self.config.allowed_openids:
            raise WxSsoError("forbidden_user", "This WeChat account is not authorized to access this memory store.", 403)

        now_int = int(time.time())
        max_exp = now_int + _MAX_SESSION_SECONDS
        gw_exp = body.get("sessionExpiresAt")
        if isinstance(gw_exp, (int, float)) and gw_exp > now_int:
            # Handle ms or sec timestamps cleanly.
            gw_exp_sec = int(gw_exp // 1000) if gw_exp > 10_000_000_000 else int(gw_exp)
            expires_at = min(max_exp, gw_exp_sec)
        else:
            expires_at = max_exp

        session = SsoSession(
            openid=openid,
            unionid=user.get("unionid") if isinstance(user.get("unionid"), str) else None,
            nickname=user.get("nickname") if isinstance(user.get("nickname"), str) else None,
            avatar_url=user.get("avatarUrl") if isinstance(user.get("avatarUrl"), str) else None,
            issuer=self.config.issuer,
            expires_at=expires_at,
        )
        token = self._sign_session(session)
        clear_tx = _format_cookie(_TX_COOKIE, "", max_age=0, secure=self.config.is_https)
        set_session = _format_cookie(
            _SESSION_COOKIE,
            token,
            max_age=max(60, expires_at - now_int),
            secure=self.config.is_https,
        )
        return tx.return_to, (clear_tx, set_session)

    def read_session(self, headers: Mapping[str, str] | None) -> SsoSession | None:
        raw = _extract_cookie(headers, _SESSION_COOKIE)
        if not raw or "." not in raw:
            return None
        payload_b64, sig_hex = raw.rsplit(".", 1)
        expected = hmac.new(
            self._session_key, payload_b64.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_hex):
            return None
        try:
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        exp = data.get("expires_at")
        openid = data.get("openid")
        if not isinstance(exp, int) or exp <= int(time.time()) or not isinstance(openid, str):
            return None
        if self.config.allowed_openids and openid not in self.config.allowed_openids:
            return None
        return SsoSession(
            openid=openid,
            unionid=data.get("unionid"),
            nickname=data.get("nickname"),
            avatar_url=data.get("avatar_url"),
            issuer=str(data.get("issuer") or self.config.issuer),
            expires_at=exp,
        )

    def logout_cookie(self) -> str:
        return _format_cookie(_SESSION_COOKIE, "", max_age=0, secure=self.config.is_https)

    def _sign_session(self, session: SsoSession) -> str:
        raw = json.dumps(
            {
                "openid": session.openid,
                "unionid": session.unionid,
                "nickname": session.nickname,
                "avatar_url": session.avatar_url,
                "issuer": session.issuer,
                "expires_at": session.expires_at,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        sig = hmac.new(self._session_key, b64.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{b64}.{sig}"

    def _prune_locked(self, now: float) -> None:
        expired = [k for k, v in self._pending.items() if v.expires_at < now]
        for key in expired:
            self._pending.pop(key, None)


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _sanitize_return_to(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return "/"
    return path


def _extract_cookie(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    cookie_header = ""
    for k, v in headers.items():
        if str(k).casefold() == "cookie":
            cookie_header = str(v)
            break
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _format_cookie(name: str, value: str, *, max_age: int, secure: bool) -> str:
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _default_token_exchange(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            data = json.loads(err.read().decode("utf-8"))
        except Exception:
            data = {"ok": False}
        return err.code, data
