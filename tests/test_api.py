"""Offline contract and token-lifecycle tests; never contact a Silo server."""

import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources" / "lib"))

from silo.api import ApiError, SiloClient, UrllibTransport, _NoRedirect, normalize_server_url
from silo.storage import SessionStore, StorageError


BASE = "https://silo.example/prefix"


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request: " + method + " " + url)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def response(data=None, status=200, headers=None):
    return status, headers or {}, data


def credentials(**extra):
    result = {"server_url": BASE, "access_token": "old-access", "refresh_token": "old-refresh",
              "expires_at": 10000, "profile_id": "p1", "profile_token": "proof"}
    result.update(extra)
    return result


def tokens(access="new-access", refresh="new-refresh"):
    return {"access_token": access, "refresh_token": refresh, "expires_in": 3600}


class ClientTests(unittest.TestCase):
    def client(self, transport, store=None):
        return SiloClient(BASE, store if store is not None else credentials(), transport, clock=lambda: 100)

    def test_device_id_rides_every_request_and_settings_use_the_settings_api(self):
        transport = Transport(response({"items": [{"key": "ui.theme_music_enabled", "value": True},
                                                  {"key": "playback.buffering_quality_mode", "value": "ask"}]}),
                              response(None, 204), response(None, 204), response(None, 204))
        client = SiloClient(BASE, credentials(), transport, clock=lambda: 100, device_id="kodi-abc")
        values = client.effective_settings(["ui.theme_music_enabled", "playback.buffering_quality_mode"])
        self.assertEqual(values, {"ui.theme_music_enabled": True, "playback.buffering_quality_mode": "ask"})
        method, url, headers, _body = transport.calls[0]
        self.assertEqual(headers["X-Silo-Device-Id"], "kodi-abc")
        self.assertIn("keys=ui.theme_music_enabled&keys=playback.buffering_quality_mode", url)
        client.put_setting("ui.theme_music_loop", True)
        method, url, _headers, body = transport.calls[1]
        self.assertEqual((method, url, body), ("PUT", BASE + "/api/v2/settings/values/ui.theme_music_loop"
                                               "?scope=profile_device", {"value": True}))
        client.save_track_pref("audio", "series:1", {"audio_track_index": 0})
        client.forget_track_pref("subtitle", "movie:1")
        self.assertEqual([(m, u) for m, u, _h, _b in transport.calls[2:]],
                         [("PUT", BASE + "/api/v2/audio-prefs/series%3A1"),
                          ("DELETE", BASE + "/api/v2/subtitle-prefs/movie%3A1")])

    def test_reverse_proxy_prefix_and_encoded_catalog_id(self):
        transport = Transport(response({"content_id": "movie:a/b"}))
        self.client(transport).item("movie:a/b")
        self.assertEqual(transport.calls[0][1], BASE + "/api/v2/catalog/items/movie%3Aa%2Fb")

    def test_public_discovery_sends_no_credentials(self):
        transport = Transport(response({"api_major": 2}), response({"password_login": True}), response({"state": "available"}))
        client = self.client(transport)
        client.server_info()
        client.auth_providers()
        client.device_capability()
        for _, _, headers, _ in transport.calls:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("X-Profile-Id", headers)
            self.assertNotIn("X-Profile-Token", headers)

    def test_login_stores_pair_atomically_and_clears_old_profile(self):
        data = dict(tokens(), user={"id": "u1"})
        store = credentials()
        transport = Transport(response(data))
        self.client(transport, store).login("alice", "secret", provider="local")
        self.assertEqual(transport.calls[0][3], {"username": "alice", "password": "secret", "provider": "local"})
        self.assertEqual(store["access_token"], "new-access")
        self.assertEqual(store["refresh_token"], "new-refresh")
        self.assertEqual(store["expires_at"], 3700)
        self.assertIsNone(store["profile_token"])
        self.assertEqual(store["auth_method"], "password")
        self.assertNotIn("password", store)

    def test_failed_login_does_not_replace_existing_session(self):
        store = credentials()
        transport = Transport(response({"detail": "Invalid credentials"}, 401))
        with self.assertRaises(ApiError):
            self.client(transport, store).login("alice", "wrong")
        self.assertEqual(store["access_token"], "old-access")

    def test_expiring_token_rotates_before_media_headers_are_created(self):
        store = credentials(expires_at=120)
        transport = Transport(response(tokens()))
        headers = self.client(transport, store).headers()
        self.assertEqual(headers["Authorization"], "Bearer new-access")
        self.assertEqual(headers["X-Profile-Token"], "proof")
        self.assertEqual(transport.calls[0][3], {"refresh_token": "old-refresh"})
        self.assertNotIn("Authorization", transport.calls[0][2])

    def test_401_refresh_retries_once_with_current_token_and_profile(self):
        transport = Transport(response({"detail": "expired"}, 401), response(tokens()), response({"sections": []}))
        store = credentials()
        self.assertEqual(self.client(transport, store).hubs(), {"sections": []})
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(transport.calls[2][2]["Authorization"], "Bearer new-access")
        self.assertEqual(transport.calls[2][2]["X-Profile-Id"], "p1")

    def test_second_401_is_not_retried(self):
        transport = Transport(response({}, 401), response(tokens()), response({}, 401))
        with self.assertRaises(ApiError) as error:
            self.client(transport).me()
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(len(transport.calls), 3)

    def test_refresh_rejection_clears_session_but_keeps_method_and_installation(self):
        store = credentials(auth_method="device", installation_id="stable")
        transport = Transport(response({"detail": "Revoked", "type": "https://x/session_revoked"}, 401))
        with self.assertRaises(ApiError):
            self.client(transport, store).refresh()
        self.assertIsNone(store["access_token"])
        self.assertIsNone(store["profile_token"])
        self.assertEqual(store["auth_method"], "device")
        self.assertEqual(store["installation_id"], "stable")

    def test_refresh_network_failure_preserves_credentials_for_retry(self):
        store = credentials()
        transport = Transport(ApiError(0, "Offline"))
        with self.assertRaises(ApiError):
            self.client(transport, store).refresh()
        self.assertEqual(store["refresh_token"], "old-refresh")

    def test_reporting_clone_has_short_transport_and_preserves_rotation_lock(self):
        client = self.client(UrllibTransport())
        clone = client.reporting_client(timeout=1.5)
        self.assertIs(clone.store, client.store)
        self.assertIs(clone._lock, client._lock)
        self.assertIsNot(clone.transport, client.transport)
        self.assertEqual(clone.transport.timeout, 1.5)
        self.assertEqual(client.transport.timeout, 20)

    def test_late_reporting_rejects_new_login_profile_proof_or_server_before_http(self):
        for changed in ({"auth_session_id": "new-login"}, {"profile_id": "p2"},
                        {"profile_token": "new-proof"}, {"server_url": "https://other.example"}):
            with self.subTest(changed=changed):
                transport = Transport()
                store = credentials()
                clone = self.client(transport, store).reporting_client()
                store.update(changed)
                with self.assertRaises(ApiError) as error:
                    clone.request("DELETE", "/playback/session", {})
                self.assertEqual(error.exception.code, "playback_scope_changed")
                self.assertEqual(transport.calls, [])

    def test_old_refresh_response_cannot_overwrite_new_login_credentials(self):
        store = credentials(auth_session_id="old-login")

        def changed_during_refresh(method, url, headers, body):
            store.update(auth_session_id="new-login", access_token="new-login-access", refresh_token="new-login-refresh")
            return response(tokens("stale-access", "stale-refresh"))

        clone = self.client(changed_during_refresh, store).reporting_client()
        with self.assertRaises(ApiError) as error:
            clone.refresh()
        self.assertEqual(error.exception.code, "playback_scope_changed")
        self.assertEqual(store["access_token"], "new-login-access")
        self.assertEqual(store["refresh_token"], "new-login-refresh")

    def test_old_refresh_rejection_cannot_clear_new_login_credentials(self):
        store = credentials(auth_session_id="old-login")

        def changed_during_refresh(method, url, headers, body):
            store.update(auth_session_id="new-login", access_token="new-login-access", refresh_token="new-login-refresh")
            return response({"detail": "Old session revoked"}, 401)

        clone = self.client(changed_during_refresh, store).reporting_client()
        with self.assertRaises(ApiError):
            clone.refresh()
        self.assertEqual(store["access_token"], "new-login-access")

    def test_clients_for_the_same_session_store_share_auth_lock_but_not_storage_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(os.path.join(directory, "session.json"))
            first, second = SiloClient(BASE, store), SiloClient(BASE, store)
            self.assertIs(first._lock, second._lock)
            self.assertIsNot(first._lock, store._lock)

    def test_verify_pin_has_no_profile_headers_and_never_replays(self):
        transport = Transport(response({"type": "https://x/pin_refused", "detail": "Wrong PIN"}, 403))
        with self.assertRaises(ApiError) as error:
            self.client(transport).verify_pin("locked/id", "1234")
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(error.exception.code, "pin_refused")
        self.assertEqual(len(transport.calls), 1)
        _, url, headers, body = transport.calls[0]
        self.assertTrue(url.endswith("/profiles/locked%2Fid/verify-pin"))
        self.assertEqual(body, {"pin": "1234"})
        self.assertNotIn("X-Profile-Id", headers)
        self.assertNotIn("X-Profile-Token", headers)

    def test_pin_401_does_not_replay(self):
        transport = Transport(response({}, 401))
        with self.assertRaises(ApiError):
            self.client(transport).verify_pin("p1", "1234")
        self.assertEqual(len(transport.calls), 1)

    def test_select_profile_discards_previous_pin_proof(self):
        store = credentials()
        client = self.client(Transport(), store)
        client.select_profile("p2")
        self.assertEqual(client.headers()["X-Profile-Id"], "p2")
        self.assertNotIn("X-Profile-Token", client.headers())

    def test_profiles_and_account_do_not_send_stale_profile_headers(self):
        transport = Transport(response({"items": []}), response({"id": "u1"}))
        client = self.client(transport)
        client.profiles()
        client.me()
        self.assertTrue(all("X-Profile-Id" not in call[2] for call in transport.calls))

    def test_logout_local_cleanup_even_when_server_offline(self):
        store = credentials(auth_method="device")
        with self.assertRaises(ApiError):
            self.client(Transport(ApiError(0, "Offline")), store).logout()
        self.assertIsNone(store["access_token"])
        self.assertEqual(store["auth_method"], "device")

    def test_switching_servers_never_reuses_previous_credentials(self):
        store = credentials()
        client = SiloClient("https://other.example", store, Transport())
        self.assertIsNone(store["access_token"])
        with self.assertRaises(ApiError):
            client.headers()

    def test_search_filters_and_cursor_use_actual_contract(self):
        transport = Transport(response({"items": [], "page": {"has_more": False}}))
        self.client(transport).search("a & b", library_id="l1", cursor="opaque/+", content_rating=["R", "PG"], skip_total=True)
        query = parse_qs(urlsplit(transport.calls[0][1]).query)
        self.assertEqual(query["q"], ["a & b"])
        self.assertEqual(query["type"], ["video"])
        self.assertEqual(query["cursor"], ["opaque/+"])
        self.assertEqual(query["content_rating"], ["R", "PG"])
        self.assertEqual(query["skip_total"], ["true"])

    def test_children_normalizes_season_and_episode_type_without_changing_ids(self):
        transport = Transport(response({"items": [{"content_id": "series:x-S01", "season_number": 1}]}),
                              response({"items": [{"content_id": "episode:x-s01e01"}]}))
        client = self.client(transport)
        seasons = client.children({"content_id": "series:x", "type": "series"})
        self.assertEqual(seasons["items"][0]["type"], "season")
        episodes = client.children(seasons["items"][0])
        self.assertEqual(episodes["items"][0]["type"], "episode")
        self.assertTrue(transport.calls[1][1].endswith("/catalog/items/series%3Ax-S01/episodes"))

    def test_presigned_images_do_not_include_bearer_credentials(self):
        client = self.client(Transport())
        self.assertEqual(client.image_url({"poster_url": "https://cdn.example/image?signature=abc"}),
                         "https://cdn.example/image?signature=abc")
        self.assertEqual(client.image_url({"still_url": "art/a.jpg"}), BASE + "/art/a.jpg")
        self.assertEqual(client.image_url({"poster_url": "file:///etc/passwd"}), "")
        self.assertEqual(client.image_url({"poster_url": "//other.example/a"}), "")
        self.assertEqual(client.image_url({"poster_url": "https://cdn.example/a|Authorization=secret"}), "")

    def test_device_approval_saves_tokens_and_bound_profile(self):
        store = credentials()
        approved = {"status": "approved", "tokens": dict(tokens(), user={"id": "u1"}),
                    "profile_id": "p2", "profile_token": "new-proof"}
        transport = Transport(response(approved))
        self.client(transport, store).device_poll("secret-device-code")
        self.assertEqual(store["auth_method"], "device")
        self.assertEqual(store["profile_id"], "p2")
        self.assertEqual(store["profile_token"], "new-proof")
        self.assertNotIn("Authorization", transport.calls[0][2])

    def test_temporary_approval_retains_bound_proof_and_refuses_profile_switch(self):
        store = credentials()
        approved = {"status": "approved", "tokens": tokens(), "temporary": True,
                    "profile_id": "p2", "profile_token": "bound-proof",
                    "session_expires_at": "2026-10-22T18:00:00Z"}
        client = self.client(Transport(response(approved)), store)
        client.device_poll("secret-device-code")
        self.assertTrue(store["temporary_session"])
        self.assertEqual(store["bound_profile_id"], "p2")
        client.select_profile("p2")
        self.assertEqual(store["profile_token"], "bound-proof")
        with self.assertRaises(ApiError) as error:
            client.select_profile("p3")
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(store["profile_id"], "p2")

    def test_temporary_approval_without_bound_profile_is_not_saved(self):
        store = credentials()
        approved = {"status": "approved", "tokens": tokens(), "temporary": True, "profile_id": ""}
        with self.assertRaises(ApiError):
            self.client(Transport(response(approved)), store).device_poll("secret")
        self.assertEqual(store["access_token"], "old-access")

    def test_full_login_clears_temporary_session_restrictions(self):
        store = credentials(temporary_session=True, bound_profile_id="p2", session_expires_at="old")
        self.client(Transport(response(tokens())), store).login("alice", "secret")
        self.assertFalse(store["temporary_session"])
        self.assertIsNone(store["bound_profile_id"])
        self.assertIsNone(store["session_expires_at"])

    def test_invalid_token_pair_preserves_existing_pair(self):
        for invalid in [{"access_token": "half"}, dict(tokens(), expires_in=float("nan")), dict(tokens(), expires_in=-1)]:
            with self.subTest(invalid=invalid):
                store = credentials()
                with self.assertRaises(ApiError):
                    self.client(Transport(response(invalid)), store).refresh()
                self.assertEqual(store["refresh_token"], "old-refresh")

    def test_url_validation_and_absolute_api_path_rejection(self):
        for invalid in ["ftp://example", "https://user:pw@example", "https://example?token=x", "https://example#secret", "https://example:99999"]:
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                normalize_server_url(invalid)
        client = self.client(Transport())
        for path in ["https://other.example/api/v2/catalog", "//other.example", "/catalog#fragment"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.request("GET", path)


class StoreTests(unittest.TestCase):
    def test_private_atomic_persistence_and_defensive_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "session.json")
            store = SessionStore(path)
            store.update({"access_token": "secret", "refresh_token": "next", "user": {"id": "u1"}})
            self.assertEqual(SessionStore(path).get("refresh_token"), "next")
            user = store.get("user")
            user["id"] = "mutated"
            self.assertEqual(store.get("user")["id"], "u1")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["session.json"])

    def test_failed_atomic_replace_preserves_previous_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "session.json")
            store = SessionStore(path)
            store.update({"access_token": "old", "refresh_token": "old-refresh"})
            with patch("silo.storage.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(StorageError):
                    store.update({"access_token": "new", "refresh_token": "new-refresh"})
            self.assertEqual(store.get("access_token"), "old")
            self.assertEqual(SessionStore(path).get("refresh_token"), "old-refresh")
            self.assertEqual(os.listdir(directory), ["session.json"])

    def test_corrupt_file_is_reported_without_printing_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "session.json")
            Path(path).write_text("not-json-secret", encoding="utf-8")
            with self.assertRaises(StorageError) as error:
                SessionStore(path)
            self.assertNotIn("secret", str(error.exception))

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "Platform lacks no-follow file opening")
    def test_symlink_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = Path(directory) / "session.json"
            link.symlink_to(target)
            with self.assertRaises(StorageError):
                SessionStore(link)


class TransportTests(unittest.TestCase):
    def test_redirect_does_not_forward_authentication_or_password(self):
        request = Request("https://silo.example/api/v2/auth/login", data=b"password=secret",
                          headers={"Authorization": "Bearer token"})
        result = _NoRedirect().redirect_request(request, None, 307, "Temporary Redirect", {},
                                                "https://other.example/collect")
        self.assertIsNone(result)

    def test_json_error_body_and_status_are_preserved(self):
        transport = UrllibTransport()
        error = HTTPError("https://silo.example/api/v2/catalog", 403, "Forbidden",
                          {"Retry-After": "15"}, io.BytesIO(b'{"detail":"Profile locked"}'))
        with patch.object(transport.opener, "open", side_effect=error):
            status, headers, body = transport.request("GET", error.url, {}, None)
        self.assertEqual(status, 403)
        self.assertEqual(headers["Retry-After"], "15")
        self.assertEqual(body["detail"], "Profile locked")

    def test_transport_connection_failure_is_actionable_and_hides_url(self):
        transport = UrllibTransport()
        with patch.object(transport.opener, "open", side_effect=URLError("https://private-host/secret")):
            with self.assertRaises(ApiError) as error:
                transport.request("GET", "https://silo.example/api/v2/catalog", {}, None)
        self.assertEqual(error.exception.status, 0)
        self.assertNotIn("private-host", str(error.exception))

    def test_response_read_timeout_is_wrapped_without_exposing_url(self):
        transport = UrllibTransport()
        response_object = type("Response", (), {"__enter__": lambda self: self,
                                                "__exit__": lambda *args: None,
                                                "read": lambda *args: (_ for _ in ()).throw(TimeoutError("https://secret.example"))})()
        with patch.object(transport.opener, "open", return_value=response_object):
            with self.assertRaises(ApiError) as error:
                transport.request("GET", "https://silo.example/api/v2/catalog", {}, None)
        self.assertEqual(error.exception.status, 0)
        self.assertEqual(error.exception.code, "connection_failed")
        self.assertNotIn("secret.example", str(error.exception))


class DownloadTransport(Transport):
    """A transport that also serves raw downloads, recorded like requests."""

    def download(self, url, headers, limit):
        self.calls.append(("DOWNLOAD", url, headers, limit))
        if not self.responses:
            raise AssertionError("Unexpected download: " + url)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class DownloadTests(unittest.TestCase):
    def client(self, transport, store=None, **kwargs):
        return SiloClient(BASE, store if store is not None else credentials(), transport,
                          clock=lambda: 100, **kwargs)

    def test_download_returns_bytes_with_this_sign_in_and_profile_headers(self):
        transport = DownloadTransport(response(b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"))
        data = self.client(transport).download("/api/v2/stream/s1/subtitles/2.srt?file_id=f", 1024)
        self.assertTrue(data.startswith(b"1\n"))
        method, url, headers, limit = transport.calls[0]
        self.assertEqual(method, "DOWNLOAD")
        self.assertEqual(urlsplit(url).hostname, "silo.example")
        self.assertEqual(headers["Authorization"], "Bearer old-access")
        self.assertEqual(headers["X-Profile-Id"], "p1")
        self.assertEqual(headers["X-Profile-Token"], "proof")
        self.assertEqual(limit, 1024)

    def test_download_refuses_other_origins_before_any_http(self):
        for url in ("https://evil.example/sub.srt", "//evil.example/sub.srt",
                    "http://silo.example/sub.srt", "https://silo.example:8443/sub.srt",
                    "https://user:pass@silo.example/sub.srt", "", "/sub.srt|X=1", "/a\r\nb"):
            with self.subTest(url=url):
                transport = DownloadTransport()
                with self.assertRaises(ApiError):
                    self.client(transport).download(url, 1024)
                self.assertEqual(transport.calls, [])

    def test_download_refreshes_once_on_401_then_retries_with_the_new_token(self):
        transport = DownloadTransport(response(None, 401), response(tokens()), response(b"WEBVTT\n"))
        data = self.client(transport).download("/api/v2/stream/s1/subtitles/2.vtt", 1024)
        self.assertEqual(data, b"WEBVTT\n")
        self.assertEqual([call[0] for call in transport.calls], ["DOWNLOAD", "POST", "DOWNLOAD"])
        self.assertEqual(transport.calls[-1][2]["Authorization"], "Bearer new-access")

    def test_download_errors_keep_the_status(self):
        transport = DownloadTransport(response(None, 404))
        with self.assertRaises(ApiError) as error:
            self.client(transport).download("/api/v2/stream/s1/subtitles/9.srt", 1024)
        self.assertEqual(error.exception.status, 404)

    def test_transport_without_download_support_is_reported(self):
        with self.assertRaises(ApiError) as error:
            self.client(Transport()).download("/api/v2/stream/s1/subtitles/2.srt", 1024)
        self.assertEqual(error.exception.code, "download_unsupported")

    def test_urllib_download_enforces_the_size_limit(self):
        class Body(io.BytesIO):
            code = 200
            headers = {}

        transport = UrllibTransport()
        with patch.object(transport.opener, "open", return_value=Body(b"x" * 11)):
            with self.assertRaises(ApiError):
                transport.download("https://silo.example/a", {}, 10)
        with patch.object(transport.opener, "open", return_value=Body(b"x" * 10)):
            self.assertEqual(transport.download("https://silo.example/a", {}, 10)[2], b"x" * 10)

    def test_user_agent_names_the_add_on_and_can_carry_its_version(self):
        transport = Transport(response({}), response({}))
        self.client(transport).me()
        self.client(transport, user_agent="FoundersTheater/0.2.2").me()
        self.assertEqual(transport.calls[0][2]["User-Agent"], "FoundersTheater")
        self.assertEqual(transport.calls[1][2]["User-Agent"], "FoundersTheater/0.2.2")


if __name__ == "__main__":
    unittest.main()
