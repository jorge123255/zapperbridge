"""Static review of the Dockerfile (no docker, no network) + the healthcheck
script against an in-process loopback server."""
import importlib.util
import os
import re
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import hdhr_emulator as h


def _load_hc():
    spec = importlib.util.spec_from_file_location(
        "zb_healthcheck", os.path.join(ROOT, "docker", "healthcheck.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestDockerfile(unittest.TestCase):
    def setUp(self):
        self.txt = open(os.path.join(ROOT, "Dockerfile")).read()
        self.lines = [l.strip() for l in self.txt.splitlines()]

    def test_base_pinned_tag_and_digest(self):
        frm = [l for l in self.lines if l.startswith("FROM ")]
        self.assertEqual(len(frm), 1)
        self.assertRegex(frm[0], r"^FROM python:3\.\d+\.\d+-slim-\w+@sha256:[0-9a-f]{64}$")
        self.assertNotIn(":latest", self.txt)

    def test_copies_everything_the_runtime_imports(self):
        self.assertIn("COPY ui /app/ui", self.txt)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "webui.py")))
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "ui", "index.html")))
        self.assertIn("docker/healthcheck.py", self.txt)
        self.assertNotRegex(self.txt, r"(?m)^ADD ")

    def test_healthcheck_nonroot_labels(self):
        self.assertRegex(self.txt, r"(?m)^HEALTHCHECK ")
        self.assertIn("USER zapper", self.txt)
        for k in ("source", "version", "revision", "created", "title", "description"):
            self.assertIn("org.opencontainers.image.%s=" % k, self.txt)
        self.assertIn("image.licenses=\"MIT\"", self.txt)

    def test_dockerignore_keeps_internal_notes_out(self):
        di = open(os.path.join(ROOT, ".dockerignore")).read().split()
        for p in ("tests/", ".git", "*.pcap"):
            self.assertIn(p, di)
        self.assertNotIn("ui", di)


class TestHealthcheck(unittest.TestCase):
    def test_up_and_down(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), h.Json)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = os.environ.get("ZB_HEALTH_PORT")
        try:
            hc = _load_hc()
            os.environ["ZB_HEALTH_PORT"] = str(srv.server_address[1])
            self.assertEqual(hc.main(), 0)
            srv.shutdown()
            srv.server_close()
            self.assertEqual(hc.main(), 1)
        finally:
            if old is None:
                os.environ.pop("ZB_HEALTH_PORT", None)
            else:
                os.environ["ZB_HEALTH_PORT"] = old


class TestHealthz(unittest.TestCase):
    def _serve(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), h.Json)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_healthz_static_no_m1(self):
        import json
        import urllib.request
        calls = []
        orig = h.get_lineup
        h.get_lineup = lambda *a, **k: calls.append(1) or []
        try:
            port = self._serve()
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(json.load(r), {"ok": True})
        finally:
            h.get_lineup = orig
        self.assertEqual(calls, [])

    def test_dockerfile_and_script_use_healthz(self):
        self.assertIn("/healthz", open(os.path.join(ROOT, "docker", "healthcheck.py")).read())
        self.assertNotIn("discover.json", open(os.path.join(ROOT, "docker", "healthcheck.py")).read().split('"""', 2)[2])

    def test_j_swallows_broken_pipe(self):
        class W:
            def write(self, b): raise BrokenPipeError()
        o = h.Json.__new__(h.Json)
        o.wfile = W()
        o.send_response = lambda *a: None
        o.send_header = lambda *a: None
        o.end_headers = lambda: None
        o._j({"a": 1})
        o._t("x")


if __name__ == "__main__":
    unittest.main()
