"""Private, atomic local session storage; no Kodi dependency."""

import copy
import json
import os
import stat
import tempfile
import threading


class StorageError(Exception):
    """The session file could not be safely read or written."""


class SessionStore:
    """JSON key/value store for one server session.

    Tokens are stored on the local filesystem, with mode 0600 on POSIX. This
    protects against other ordinary users; it is not an encrypted credential
    vault. Writes replace the complete file atomically, including token rotation.
    """

    def __init__(self, path):
        self.path = os.path.abspath(os.fspath(path))
        self._lock = threading.RLock()
        self._data = self._read()

    def _read(self):
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise StorageError("Session storage must be a regular file.")
                value = json.load(handle)
            if not isinstance(value, dict):
                raise StorageError("Session storage must contain a JSON object.")
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise StorageError("Could not read the local session file.") from exc

    def _write(self, value):
        parent = os.path.dirname(self.path)
        tmp = None
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            if os.path.islink(self.path):
                raise StorageError("Session storage must not be a symbolic link.")
            fd, tmp = tempfile.mkstemp(prefix=".silo-session-", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                if os.name == "posix":
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            tmp = None
        except (OSError, ValueError, TypeError) as exc:
            raise StorageError("Could not save the local session file.") from exc
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def get(self, key, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key, value):
        self.update({key: value})

    def update(self, values):
        with self._lock:
            updated = copy.deepcopy(self._data)
            updated.update(values)
            self._write(updated)
            self._data = updated

    def delete(self, key):
        with self._lock:
            updated = copy.deepcopy(self._data)
            updated.pop(key, None)
            self._write(updated)
            self._data = updated

    def clear(self):
        with self._lock:
            self._write({})
            self._data = {}
