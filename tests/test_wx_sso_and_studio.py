from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from hermes_topic_memory.panel_app import PanelApplication
from hermes_topic_memory.panel_server import create_panel_server
from hermes_topic_memory.reader import ReaderRequest, ReaderResponse
from hermes_topic_memory.topic_admin import TopicAdminService
from hermes_topic_memory.wx_sso import WxGatewaySso, WxSsoConfig


class _StubReader:
    def read(self, request: ReaderRequest) -> ReaderResponse:
        return ReaderResponse(answer="stub capsule", model="stub")


def test_wx_sso_end_to_end_and_studio_ui(tmp_path: Path) -> None:
    admin = TopicAdminService(tmp_path)
    admin.create_topic("veloiq", "VeloIQ", ("veloiq",), "# VeloIQ\n- rule 1\n")

    captured_exchange: list[dict[str, Any]] = []
    secret = "deadbeef0123456789abcdef"

    def fake_exchange(
        url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        captured_exchange.append({"url": url, "payload": payload, "headers": headers})
        # Verify exact HMAC-SHA256 contract from wx-gateway references/sso.md
        expected_msg = (
            f"sso-token|{payload['code']}|{payload['redirect_uri']}|"
            f"{payload['code_verifier']}|{payload['state']}|{headers['X-Ts']}|{headers['X-App-Name']}"
        )
        expected_sig = hmac.new(
            secret.encode("utf-8"), expected_msg.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(headers["X-Internal-Auth"], expected_sig)
        return 200, {
            "ok": True,
            "issuer": "https://wx.mvp.restry.cn",
            "app": "topic-memory",
            "user": {"openid": "o_dad_123", "unionid": "u_dad", "nickname": "爸爸"},
            "sessionExpiresAt": int(time.time()) + 7 * 24 * 3600,
        }

    cfg = WxSsoConfig(
        gateway_base="https://wx.mvp.restry.cn",
        app_name="topic-memory",
        app_secret=secret,
        public_base_url="https://memory.mvp.restry.cn",
        allowed_openids=("o_dad_123",),
    )
    sso = WxGatewaySso(cfg, session_secret="panel-tok", token_exchanger=fake_exchange)
    app = PanelApplication(tmp_path, _StubReader(), "panel-tok", wx_sso=sso, ui_mode="studio")
    srv = create_panel_server(app, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)

        # 1. Unauthenticated GET / seamlessly redirects to wx-gateway /sso/authorize
        conn.request("GET", "/")
        r1 = conn.getresponse()
        r1.read()
        assert r1.status == 302
        loc = r1.getheader("Location") or ""
        assert loc.startswith("https://wx.mvp.restry.cn/sso/authorize?")
        qs = parse_qs(urlsplit(loc).query)
        assert qs["client_id"] == ["topic-memory"]
        assert qs["redirect_uri"] == ["https://memory.mvp.restry.cn/api/sso/callback"]
        assert qs["code_challenge_method"] == ["S256"]
        state = qs["state"][0]
        tx_cookie = (r1.getheader("Set-Cookie") or "").split(";", 1)[0]
        assert tx_cookie.startswith("htm_sso_tx=")

        # 2. Explicit token fallback entry remains reachable without redirect loop
        conn.request("GET", "/?token_login=1")
        r_fb = conn.getresponse()
        body_fb = r_fb.read().decode("utf-8")
        assert r_fb.status == 200
        assert "Topic Memory" in body_fb

        # 3. Callback exchanges code with HMAC + mints htm_session cookie
        cb_path = f"/api/sso/callback?code=one-time-code&state={state}&iss=https://wx.mvp.restry.cn"
        conn.request("GET", cb_path, headers={"Cookie": tx_cookie})
        r2 = conn.getresponse()
        r2.read()
        assert r2.status == 303
        assert r2.getheader("Location") == "/"
        set_cookies = r2.headers.get_all("Set-Cookie") or []
        session_cookie = next(c for c in set_cookies if c.startswith("htm_session=")).split(";", 1)[0]
        assert len(captured_exchange) == 1

        # 4. Authenticated GET / now serves Studio UI directly (no redirect)
        conn.request("GET", "/", headers={"Cookie": session_cookie})
        r3 = conn.getresponse()
        html = r3.read().decode("utf-8")
        assert r3.status == 200
        assert "/studio/app.js" in html

        # 5. Session cookie authorizes /api/auth/me, /api/overview, /api/v1/topics, /api/dreams
        conn.request("GET", "/api/auth/me", headers={"Cookie": session_cookie})
        me = json.loads(conn.getresponse().read().decode("utf-8"))
        assert me["authenticated"] is True and me["mode"] == "sso" and me["user"]["nickname"] == "爸爸"

        conn.request("GET", "/api/dreams", headers={"Cookie": session_cookie})
        dr = conn.getresponse()
        assert dr.status == 200 and json.loads(dr.read().decode("utf-8")) == {"items": []}

        # 6. Logout clears cookie and returns central logout URL
        conn.request("POST", "/api/sso/logout", headers={"Cookie": session_cookie})
        r_out = conn.getresponse()
        out_json = json.loads(r_out.read().decode("utf-8"))
        assert r_out.status == 200 and out_json["ok"] is True
        assert out_json["central_logout_url"] == "https://wx.mvp.restry.cn/sso/logout"
    finally:
        srv.shutdown()
        srv.server_close()
