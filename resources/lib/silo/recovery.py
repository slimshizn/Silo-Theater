"""Private, bounded cleanup of playback left behind by an earlier Kodi process."""

import copy
import math
import os
import threading
import time
import uuid
from urllib.parse import quote

from .api import ApiError
from .storage import StorageError


JOURNAL_KEY = "pending_playback"
MAX_PENDING = 64
MAX_RECOVERY_STOPS = 2
RECOVERY_BUDGET = 4.5
_fallback_lock = threading.RLock()


def _guard(client):
    return getattr(client, "_lock", None) or _fallback_lock


def _context(client, create=False):
    store = client.store
    server = client.server_url
    if store.get("server_url", server) != server or not store.get("profile_id"):
        return None
    identity = store.get("auth_session_id")
    if not identity and create:
        identity = str(uuid.uuid4())
        store.update({"auth_session_id": identity})
    if not identity:
        return None
    return {"server_url": server, "profile_id": str(store.get("profile_id")),
            "auth_session_id": identity}


def _same_context(record, context):
    return bool(isinstance(record, dict) and context and all(
        record.get(key) == value for key, value in context.items()))


def _entries(value):
    if not isinstance(value, dict):
        raise StorageError("The stored playback cleanup data is invalid.")
    return value


def _change(store, transform):
    def apply(value):
        return transform(_entries(value))
    mutate = getattr(store, "mutate", None)
    if mutate is not None:
        return mutate(JOURNAL_KEY, apply, {})
    # Lightweight injected stores remain usable offline. Production uses the
    # SessionStore transaction above, shared with every other local writer.
    with _fallback_lock:
        value = apply(copy.deepcopy(store.get(JOURNAL_KEY, {})))
        store.update({JOURNAL_KEY: value})
        return copy.deepcopy(value)


def remember_session(client, installation_id, session_id):
    """Persist a session before opening media, without copying credentials."""
    with _guard(client):
        context = _context(client, create=True)
        if not context or not isinstance(installation_id, str) or not installation_id or not isinstance(session_id, str) or not session_id:
            raise StorageError("Playback cleanup could not be associated with this sign-in.")
        key = str(uuid.uuid4())
        record = dict(context, installation_id=installation_id, session_id=session_id,
                      owner_pid=os.getpid())

        def add(entries):
            if len(entries) >= MAX_PENDING:
                raise StorageError("There are too many unfinished playback sessions. Restart Kodi to retry cleanup before playing another title.")
            entries[key] = record
            return entries

        _change(client.store, add)
        return key


def _stop_body(body, installation_id):
    if not isinstance(body, dict) or body.get("installation_id") != installation_id:
        return None
    identity = body.get("stop_id")
    try:
        parsed = uuid.UUID(identity)
        if parsed.int == 0 or str(parsed) != identity:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    result = {"installation_id": installation_id, "stop_id": identity}
    if ("sequence" in body) != ("position" in body):
        return None
    if "sequence" in body:
        sequence, position = body["sequence"], body["position"]
        if (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0
                or not isinstance(position, (int, float)) or isinstance(position, bool)
                or not math.isfinite(position) or position < 0):
            return None
        result.update(sequence=sequence, position=position)
    if "is_paused" in body:
        if not isinstance(body["is_paused"], bool):
            return None
        result["is_paused"] = body["is_paused"]
    return result


def remember_stop(client, key, body):
    """Record the first stop body before HTTP; retries reuse it unchanged."""
    result = None
    with _guard(client):
        context = _context(client)

        def update(entries):
            nonlocal result
            record = entries.get(key)
            if _same_context(record, context):
                candidate = _stop_body(record.get("stop_body", body), record.get("installation_id"))
                if candidate is not None:
                    record["stop_body"] = candidate
                    result = copy.deepcopy(candidate)
            return entries

        _change(client.store, update)
    return result


def forget_session(client, key):
    """Remove exactly the confirmed entry, preserving all newer sessions."""
    removed = False
    with _guard(client):
        context = _context(client)

        def remove(entries):
            nonlocal removed
            if _same_context(entries.get(key), context):
                entries.pop(key)
                removed = True
            return entries

        _change(client.store, remove)
    return removed


class RecoveryWorker:
    """One short background pass. No Kodi object or user interface is used."""

    def __init__(self, client, clock=None):
        self.client = client
        self.clock = clock or time.monotonic
        self.attempted = 0
        self.completed = 0
        self.error_code = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="SiloPlaybackRecovery", daemon=True)
        self._thread.start()

    def wait(self, timeout=None):
        """Optional synchronization for tests; application startup never waits."""
        return self._done.wait(timeout)

    def _run(self):
        try:
            self._recover()
        except ApiError as error:
            self.error_code = error.code or "request_failed"
        except (StorageError, OSError, ValueError, TypeError, KeyError):
            self.error_code = "recovery_failed"
        finally:
            self._done.set()

    def _recover(self):
        deadline = self.clock() + RECOVERY_BUDGET
        # Cloning can wait for an in-flight token rotation, so do it here.
        client = self.client.reporting_client(timeout=1.5)
        with _guard(client):
            context = _context(client)
            entries = _entries(client.store.get(JOURNAL_KEY, {}))
            candidates = [(key, record) for key, record in entries.items()
                          if _same_context(record, context)
                          and isinstance(record.get("owner_pid"), int)
                          and record["owner_pid"] > 0 and record["owner_pid"] != os.getpid()
                          and isinstance(record.get("session_id"), str) and record["session_id"]]
        if not candidates or self.clock() >= deadline:
            return
        capabilities = client.request("GET", "/playback/capabilities", None)
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("installation_id"), str):
            return
        installation_id = capabilities["installation_id"]
        for key, record in candidates:
            if self.attempted >= MAX_RECOVERY_STOPS or self.clock() >= deadline:
                break
            if record.get("installation_id") != installation_id:
                continue
            body = remember_stop(client, key, {"installation_id": installation_id,
                                               "stop_id": str(uuid.uuid4())})
            if body is None:
                continue
            self.attempted += 1
            try:
                receipt = client.request("DELETE", "/playback/" + quote(record["session_id"], safe=""), body)
            except ApiError as error:
                if error.status not in (404, 410):
                    raise
            else:
                if (not isinstance(receipt, dict) or receipt.get("outcome") not in ("stopped", "replayed")
                        or receipt.get("outcome") == "stopped" and receipt.get("stop_id") != body["stop_id"]):
                    self.error_code = "invalid_stop_receipt"
                    return
            if forget_session(client, key):
                self.completed += 1


def start_recovery(client):
    """Return immediately while earlier-process sessions are checked."""
    return RecoveryWorker(client)
