"""The loopback relay plays server-served media without giving Kodi the sign-in token."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo.relay import MediaRelay

BODY = bytes(range(256)) * 4096  # 1 MiB


class Upstream(BaseHTTPRequestHandler):
    """A Silo server that only serves media to the current access token."""

    def log_message(self, format, *args):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        self.server.seen.append((self.path, dict(self.headers)))
        if self.headers.get("Authorization") != "Bearer " + self.server.valid_token:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/silo/missing"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data, status = BODY, 200
        byte_range = self.headers.get("Range")
        if byte_range:
            start, end = byte_range.split("=")[1].split("-")
            start, end = int(start), int(end) if end else len(BODY) - 1
            data, status = BODY[start:end + 1], 206
        self.send_response(status)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Set-Cookie", "secret=1")
        if status == 206:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, len(BODY)))
        self.end_headers()
        if not head:
            self.wfile.write(data)


class Store:
    def __init__(self):
        self.values = {"access_token": "old", "refresh_token": "refresh"}

    def get(self, name, default=None):
        return self.values.get(name, default)


class Client:
    def __init__(self):
        self.store = Store()
        self.refreshes = 0

    def headers(self):
        return {"Accept": "application/json", "User-Agent": "FoundersTheater",
                "Authorization": "Bearer " + self.store.values["access_token"], "X-Profile-Id": "p1"}

    def refresh_if_current(self, headers):
        self.refreshes += 1
        self.store.values["access_token"] = "new"


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.seen, self.upstream.valid_token = [], "old"
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:{}/silo".format(self.upstream.server_address[1])
        self.client = Client()
        self.relay = MediaRelay(self.client)

    def tearDown(self):
        self.relay.close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def fetch(self, url, method="GET", **headers):
        with urlopen(Request(url, headers=headers, method=method), timeout=10) as response:
            return response.status, dict(response.headers), response.read()

    def test_kodi_gets_a_loopback_url_and_the_server_gets_the_sign_in(self):
        local = self.relay.register(self.base + "/api/v2/stream/s1?st=signed", {"X-Route": "r"})
        self.assertTrue(local.startswith("http://127.0.0.1:{}/".format(self.relay.port)))
        self.assertTrue(local.endswith("/s1?st=signed"))
        self.assertNotIn("old", local)
        status, headers, data = self.fetch(local)
        self.assertEqual((status, data), (200, BODY))
        self.assertEqual(headers["Content-Length"], str(len(BODY)))
        self.assertNotIn("Set-Cookie", headers)
        path, sent = self.upstream.seen[-1]
        self.assertEqual(path, "/silo/api/v2/stream/s1?st=signed")
        self.assertEqual((sent["Authorization"], sent["X-Profile-Id"], sent["X-Route"]), ("Bearer old", "p1", "r"))

    def test_seeking_passes_byte_ranges_through(self):
        local = self.relay.register(self.base + "/api/v2/stream/s1?st=signed")
        status, headers, data = self.fetch(local, Range="bytes=1000-1999")
        self.assertEqual((status, data), (206, BODY[1000:2000]))
        self.assertEqual(headers["Content-Range"], "bytes 1000-1999/{}".format(len(BODY)))
        status, headers, data = self.fetch(local, method="HEAD")
        self.assertEqual((status, data, headers["Accept-Ranges"]), (200, b"", "bytes"))

    def test_an_expired_token_is_renewed_once_mid_playback(self):
        local = self.relay.register(self.base + "/api/v2/stream/s1?st=signed")
        self.upstream.valid_token = "new"
        status, _, data = self.fetch(local, Range="bytes=0-9")
        self.assertEqual((status, data, self.client.refreshes), (206, BODY[:10], 1))
        self.assertEqual([sent["Authorization"] for _, sent in self.upstream.seen], ["Bearer old", "Bearer new"])

    def test_a_refused_sign_in_reaches_kodi_as_the_server_answered(self):
        local = self.relay.register(self.base + "/api/v2/stream/s1?st=signed")
        self.upstream.valid_token = "other"
        with self.assertRaises(HTTPError) as caught:
            self.fetch(local)
        self.assertEqual((caught.exception.code, self.client.refreshes), (401, 1))

    def test_hls_segments_resolve_relative_to_the_playlist(self):
        local = self.relay.register(self.base + "/api/v2/playback/transcode/s1/master.m3u8?st=a")
        segment = local.split("master.m3u8")[0] + "segment/3.ts?st=b"
        self.assertEqual(self.fetch(segment)[0], 200)
        self.assertEqual(self.upstream.seen[-1][0], "/silo/api/v2/playback/transcode/s1/segment/3.ts?st=b")

    def test_unknown_routes_and_escapes_from_the_stream_directory_are_refused(self):
        local = self.relay.register(self.base + "/api/v2/stream/s1")
        prefix = local.rsplit("/", 1)[0]
        port = self.relay.port
        for url in ("http://127.0.0.1:{}/unknown/s1".format(port), prefix + "/../auth/me",
                    prefix + "/%2e%2e/auth/me", prefix + "/a%2Fb", prefix + "//etc", prefix + "/"):
            with self.subTest(url=url), self.assertRaises(HTTPError) as caught:
                self.fetch(url)
            self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.upstream.seen, [])

    def test_server_errors_pass_through(self):
        local = self.relay.register(self.base + "/missing/file")
        with self.assertRaises(HTTPError) as caught:
            self.fetch(local)
        self.assertEqual(caught.exception.code, 404)

    def test_closed_relay_stops_listening(self):
        local = self.relay.register(self.base + "/api/v2/stream/s1")
        self.relay.close()
        with self.assertRaises(URLError):
            self.fetch(local)


if __name__ == "__main__":
    unittest.main()
