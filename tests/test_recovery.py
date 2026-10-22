"""Crash cleanup stays scoped, durable, bounded, and independent of Kodi."""

import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources" / "lib"))

from silo.api import ApiError, SiloClient
from silo.recovery import (JOURNAL_KEY, MAX_PENDING, RecoveryWorker, forget_session,
                           remember_session, remember_stop, start_recovery)
from silo.storage import SessionStore, StorageError


BASE = "https://silo.invalid"
INSTALLATION = str(uuid.UUID(int=10))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="silo-recovery-test-")
        self.addCleanup(self.directory.cleanup)
        self.store = SessionStore(os.path.join(self.directory.name, "session.json"))
        self.store.update({"server_url": BASE, "access_token": "account-credential",
                           "refresh_token": "refresh-credential", "expires_at": 10 ** 12,
                           "profile_id": "profile-a", "profile_token": "pin-proof",
                           "auth_session_id": "login-a"})
        self.calls = []
        self.handler = None

        def transport(method, url, headers, body=None):
            self.calls.append((method, url, copy.deepcopy(body)))
            if self.handler is not None:
                result = self.handler(method, url, headers, body)
                if result is not None:
                    return result
            if method == "GET":
                return 200, {}, {"installation_id": INSTALLATION}
            return 200, {}, {"outcome": "stopped", "stop_id": body["stop_id"]}

        self.client = SiloClient(BASE, self.store, transport)

    def pending(self, session_id=None, previous=True, installation=INSTALLATION):
        session_id = session_id or str(uuid.uuid4())
        key = remember_session(self.client, installation, session_id)
        if previous:
            def change(entries):
                entries[key]["owner_pid"] = os.getpid() + 1000
                return entries
            self.store.mutate(JOURNAL_KEY, change)
        return key

    def recover(self, **kwargs):
        worker = RecoveryWorker(self.client, **kwargs)
        self.assertTrue(worker.wait(2), "Recovery did not finish its bounded pass")
        return worker

    def test_journal_is_private_minimal_and_stop_body_is_immutable(self):
        key = self.pending(previous=False)
        record = self.store.get(JOURNAL_KEY)[key]
        self.assertEqual(set(record), {"server_url", "profile_id", "auth_session_id",
                                       "installation_id", "session_id", "owner_pid"})
        self.assertEqual(record["owner_pid"], os.getpid())
        self.assertNotIn("credential", str(record))
        body = {"installation_id": INSTALLATION, "stop_id": str(uuid.uuid4()),
                "sequence": 7, "position": 42.5, "is_paused": True,
                "password": "must-not-be-copied"}
        saved = remember_stop(self.client, key, body)
        self.assertNotIn("password", saved)
        body["position"] = 0
        retry = remember_stop(self.client, key, {"installation_id": INSTALLATION,
                                                 "stop_id": str(uuid.uuid4())})
        self.assertEqual(saved, retry)
        self.assertEqual(self.store.get(JOURNAL_KEY)[key]["stop_body"]["position"], 42.5)

    def test_legacy_login_identity_is_created_before_journaling(self):
        self.store.delete("auth_session_id")
        key = self.pending(previous=False)
        self.assertTrue(self.store.get("auth_session_id"))
        self.assertEqual(self.store.get(JOURNAL_KEY)[key]["auth_session_id"],
                         self.store.get("auth_session_id"))

    def test_recovery_persists_stop_before_http_without_fabricating_position(self):
        key = self.pending()

        def handler(method, url, headers, body):
            if method == "DELETE":
                self.assertEqual(self.store.get(JOURNAL_KEY)[key]["stop_body"], body)
                self.assertEqual(set(body), {"installation_id", "stop_id"})
                self.assertEqual(headers["X-Profile-Id"], "profile-a")
                self.assertEqual(headers["X-Profile-Token"], "pin-proof")
            return None

        self.handler = handler
        worker = self.recover()
        self.assertEqual((worker.attempted, worker.completed), (1, 1))
        self.assertEqual([call[0] for call in self.calls], ["GET", "DELETE"])
        self.assertEqual(self.store.get(JOURNAL_KEY), {})

    def test_existing_final_sample_is_retried_exactly_after_lost_response(self):
        key = self.pending()
        body = {"installation_id": INSTALLATION, "stop_id": str(uuid.uuid4()),
                "sequence": 11, "position": 500.25, "is_paused": False}
        remember_stop(self.client, key, body)

        def fail(method, url, headers, request_body):
            if method == "DELETE":
                raise ApiError(0, "Network interrupted", "connection_failed")
            return None

        self.handler = fail
        first = self.recover()
        self.assertEqual(first.completed, 0)
        self.assertIn(key, self.store.get(JOURNAL_KEY))
        self.handler = lambda method, url, headers, request_body: (
            (200, {}, {"outcome": "replayed", "stop_id": str(uuid.uuid4())})
            if method == "DELETE" else None)
        second = self.recover()
        bodies = [call[2] for call in self.calls if call[0] == "DELETE"]
        self.assertEqual(bodies, [body, body])
        self.assertEqual(second.completed, 1)

    def test_current_process_and_other_login_profile_server_are_untouched(self):
        current = self.pending(previous=False)
        others = [self.pending() for _ in range(3)]

        def change(entries):
            entries[others[0]]["server_url"] = "https://other.invalid"
            entries[others[1]]["profile_id"] = "another-profile"
            entries[others[2]]["auth_session_id"] = "another-login"
            return entries

        self.store.mutate(JOURNAL_KEY, change)
        worker = self.recover()
        self.assertEqual(worker.attempted, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(set(self.store.get(JOURNAL_KEY)), {current, *others})

    def test_changed_installation_and_invalid_receipt_remain_pending(self):
        old = self.pending(installation=str(uuid.uuid4()))
        current = self.pending()
        self.handler = lambda method, url, headers, body: (
            (200, {}, {"outcome": "unexpected"}) if method == "DELETE" else None)
        worker = self.recover()
        self.assertEqual(worker.attempted, 1)
        self.assertEqual(worker.completed, 0)
        self.assertEqual(set(self.store.get(JOURNAL_KEY)), {old, current})

    def test_404_and_410_confirm_cleanup_but_other_errors_do_not(self):
        for status in (404, 410, 403, 500):
            with self.subTest(status=status):
                self.store.set(JOURNAL_KEY, {})
                key = self.pending()
                self.handler = lambda method, url, headers, body: (
                    (status, {}, {"type": "/playback_session_not_found"})
                    if method == "DELETE" else None)
                worker = self.recover()
                self.assertEqual(key in self.store.get(JOURNAL_KEY), status not in (404, 410))
                self.assertEqual(worker.completed, int(status in (404, 410)))

    def test_recovery_prunes_only_its_entry_when_new_playback_starts(self):
        old = self.pending()
        created = []

        def handler(method, url, headers, body):
            if method == "DELETE":
                created.append(self.pending(previous=False))
            return None

        self.handler = handler
        worker = self.recover()
        self.assertEqual(worker.completed, 1)
        self.assertNotIn(old, self.store.get(JOURNAL_KEY))
        self.assertEqual(set(self.store.get(JOURNAL_KEY)), set(created))

    def test_pass_limits_requests_and_stops_on_first_network_failure(self):
        for _ in range(4):
            self.pending()
        worker = self.recover()
        self.assertEqual((worker.attempted, worker.completed), (2, 2))
        self.assertEqual(len(self.calls), 3)
        self.calls.clear()

        def fail(method, url, headers, body):
            raise ApiError(0, "Offline", "connection_failed")

        self.handler = fail
        worker = self.recover()
        self.assertEqual(worker.attempted, 0)
        self.assertEqual(len(self.calls), 1)

    def test_budget_is_checked_after_discovery_before_any_delete(self):
        key = self.pending()
        ticks = iter((0, 0, 5))
        worker = self.recover(clock=lambda: next(ticks))
        self.assertEqual(worker.attempted, 0)
        self.assertEqual([call[0] for call in self.calls], ["GET"])
        self.assertIn(key, self.store.get(JOURNAL_KEY))

    def test_start_returns_while_background_discovery_is_blocked(self):
        self.pending()
        entered, release = threading.Event(), threading.Event()

        def handler(method, url, headers, body):
            entered.set()
            release.wait(2)
            return None

        self.handler = handler
        worker = start_recovery(self.client)
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(worker.wait(0))
        finally:
            release.set()
            self.assertTrue(worker.wait(2))

    def test_capacity_failure_keeps_existing_entries_and_changed_scope_cannot_prune(self):
        key = self.pending()
        self.store.update({"auth_session_id": "another-login"})
        self.assertFalse(forget_session(self.client, key))
        self.assertIsNone(remember_stop(self.client, key, {"installation_id": INSTALLATION,
                                                         "stop_id": str(uuid.uuid4())}))
        original = {str(index): {"pending": True} for index in range(MAX_PENDING)}
        self.store.set(JOURNAL_KEY, original)
        with self.assertRaises(StorageError):
            self.pending(previous=False)
        self.assertEqual(self.store.get(JOURNAL_KEY), original)


class AtomicStoreTests(unittest.TestCase):
    def test_concurrent_mutations_preserve_every_increment_and_private_copies(self):
        with tempfile.TemporaryDirectory(prefix="silo-mutate-test-") as directory:
            store = SessionStore(os.path.join(directory, "session.json"))
            store.set("counter", {"value": 0})
            gate = threading.Barrier(6)

            def increment():
                gate.wait()
                for _ in range(3):
                    store.mutate("counter", lambda value: {"value": value["value"] + 1})

            threads = [threading.Thread(target=increment) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(SessionStore(store.path).get("counter"), {"value": 18})
            returned = store.mutate("counter", lambda value: value)
            returned["value"] = -1
            self.assertEqual(store.get("counter"), {"value": 18})

    def test_failed_transform_or_write_preserves_memory_and_disk(self):
        with tempfile.TemporaryDirectory(prefix="silo-mutate-test-") as directory:
            store = SessionStore(os.path.join(directory, "session.json"))
            store.set("pending", {"one": 1})

            def fail(value):
                value["one"] = 2
                raise ValueError("invalid mutation")

            with self.assertRaises(ValueError):
                store.mutate("pending", fail)
            with patch.object(store, "_write", side_effect=StorageError("unavailable")):
                with self.assertRaises(StorageError):
                    store.mutate("pending", lambda value: {"two": 2})
            self.assertEqual(store.get("pending"), {"one": 1})
            self.assertEqual(SessionStore(store.path).get("pending"), {"one": 1})


if __name__ == "__main__":
    unittest.main()
