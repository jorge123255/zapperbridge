"""Optional ZUI_TOKEN for the read-only UI, plus the UI-never-hardcodes-a-Mini rule.
Offline loopback server, same harness as test_webui."""
import http.client
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_webui as tw
import webui

TOK = "s3cret-token-123"


class TestToken(tw.Base):
    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def test_no_token_configured_is_open(self):
        with tw.zui():
            self.assertEqual(self.get("/api/ui/state")[0], 200)

    def test_token_required(self):
        os.environ["ZUI_TOKEN"] = TOK
        with tw.zui():
            self.assertEqual(self.get("/api/ui/state")[0], 401)
            self.assertEqual(self.get("/ui")[0], 401)
            self.assertEqual(self.get("/ui/ui.js")[0], 401)
            st, hd, _ = self.get("/api/ui/state", {"Authorization": "Bearer wrong-token-xx"})
            self.assertEqual(st, 401)
            self.assertEqual(hd.get("WWW-Authenticate"), "Bearer")

    def test_bearer_and_cookie_accepted(self):
        os.environ["ZUI_TOKEN"] = TOK
        with tw.zui():
            self.assertEqual(self.get("/api/ui/state", {"Authorization": "Bearer " + TOK})[0], 200)
            self.assertEqual(self.get("/api/ui/state", {"Cookie": "a=b; zui_token=" + TOK})[0], 200)
            self.assertEqual(self.get("/api/ui/state", {"Cookie": "zui_token=nope"})[0], 401)

    def test_query_login_sets_cookie_only_on_ui_root(self):
        os.environ["ZUI_TOKEN"] = TOK
        with tw.zui():
            st, hd, _ = self.get("/ui?token=" + TOK)
            self.assertEqual(st, 303)
            self.assertEqual(hd["Location"], "/ui")
            self.assertIn("HttpOnly", hd["Set-Cookie"])
            self.assertIn("SameSite=Strict", hd["Set-Cookie"])
            self.assertEqual(self.get("/api/ui/state?token=" + TOK)[0], 401)
            self.assertEqual(self.get("/ui?token=wrong-token-xx")[0], 401)

    def test_short_token_fails_closed(self):
        os.environ["ZUI_TOKEN"] = "abc"
        with tw.zui():
            self.assertEqual(self.get("/api/ui/state", {"Authorization": "Bearer abc"})[0], 401)
            self.assertIsNotNone(webui.token_problem())

    def test_token_does_not_leak_into_state(self):
        os.environ["ZUI_TOKEN"] = TOK
        with tw.zui():
            _, _, body = self.get("/api/ui/state", {"Authorization": "Bearer " + TOK})
            self.assertNotIn(TOK.encode(), body)


class TestNoHardcodedMini(tw.Base):
    def test_empty_when_unconfigured(self):
        os.environ.pop("ZUI_RESERVED_IPS", None)
        with tw.zui():
            self.assertEqual(self.jget("/api/ui/devices")["protected_minis"], [])
            self.assertEqual(self.jget("/api/ui/aliases")["reserved_ips"], [])
        self.assertNotIn("192.0.2.237", open(os.path.join(tw.ROOT, "webui.py")).read())

    def test_lineup_age_message(self):
        js = open(os.path.join(tw.ROOT, "ui", "ui.js")).read()
        self.assertIn("lineup not cached yet", js)


if __name__ == "__main__":
    unittest.main()
