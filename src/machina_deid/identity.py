"""Deterministic, persistent pseudonym mapping.

Pseudonyms are derived with HMAC-SHA256 keyed by a private secret, so the same
(kind, identity) always yields the same pseudonym for a given secret, and the
pseudonym cannot be reversed or brute-forced without the secret.

Raw identities and the secret are never logged and never included in
exception messages or ``repr`` output. The persisted JSON map stores only
HMAC digests and pseudonyms, never raw identities.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unicodedata
from pathlib import Path

DEFAULT_SECRET_ENV = "MACHINA_DEID_SECRET"
MIN_SECRET_BYTES = 16
_SCHEMA_VERSION = 1

# kind -> pseudonym label prefix
KINDS = {
    "person": "Person",
    "org": "Organization",
    "email": "Email",
    "phone": "Phone",
    "domain": "Domain",
    "address": "Address",
    "account": "Account",
    "project": "Project",
    "part": "Part",
    "text": "Identity",
}


class IdentityError(Exception):
    """Raised for configuration or persistence problems (never includes secrets)."""


def load_secret(path: str | os.PathLike | None = None, env_var: str = DEFAULT_SECRET_ENV) -> bytes:
    """Load the HMAC secret from an explicit file path, or else an env var.

    The file's content (surrounding whitespace stripped) or the env var value
    is used as UTF-8 bytes. There is no default or fallback secret.
    """
    if path is not None:
        try:
            raw = Path(path).read_bytes().strip()
        except OSError as exc:
            raise IdentityError(f"cannot read secret file ({type(exc).__name__})") from None
    else:
        value = os.environ.get(env_var)
        if not value:
            raise IdentityError(f"no secret: pass a file path or set {env_var}")
        raw = value.strip().encode("utf-8")
    if len(raw) < MIN_SECRET_BYTES:
        raise IdentityError(f"secret too short (minimum {MIN_SECRET_BYTES} bytes)")
    return raw


def _normalize(identity: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", identity).casefold().split())


class PseudonymMapper:
    """Maps (kind, identity) to stable typed pseudonyms, optionally persisted."""

    def __init__(self, secret: bytes, store_path: str | os.PathLike | None = None):
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
            raise IdentityError(f"secret must be bytes, at least {MIN_SECRET_BYTES} long")
        self._secret = bytes(secret)
        self._store_path = Path(store_path) if store_path is not None else None
        # key digest (hex) -> pseudonym
        self._map: dict[str, str] = {}
        self._used: set[str] = set()
        if self._store_path is not None and self._store_path.exists():
            self._load()

    def __repr__(self) -> str:
        return f"PseudonymMapper(entries={len(self._map)})"

    def _fingerprint(self) -> str:
        # Lets us detect loading a store created with a different secret.
        return hmac.new(self._secret, b"machina-deid/fingerprint", hashlib.sha256).hexdigest()[:16]

    def _digest(self, kind: str, identity: str) -> bytes:
        msg = kind.encode("ascii") + b"\x00" + _normalize(identity).encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).digest()

    def pseudonym(self, kind: str, identity: str) -> str:
        """Return the stable pseudonym for ``identity`` of the given ``kind``."""
        if kind not in KINDS:
            raise IdentityError(f"unknown kind (allowed: {', '.join(sorted(KINDS))})")
        if not isinstance(identity, str) or not _normalize(identity):
            raise IdentityError("identity must be a non-empty string")
        digest = self._digest(kind, identity)
        key = digest.hex()
        existing = self._map.get(key)
        if existing is not None:
            return existing
        # Extend the token on the (astronomically unlikely) collision.
        for length in range(5, 33):
            token = digest.hex()[:length].upper()
            name = self._format(kind, token)
            if name not in self._used:
                break
        else:  # pragma: no cover
            raise IdentityError("could not allocate unique pseudonym")
        self._map[key] = name
        self._used.add(name)
        if self._store_path is not None:
            if getattr(self, "_defer_save", False):
                self._dirty = True
            else:
                self.save()
        return name

    def flush(self) -> None:
        """Persist deferred additions (one atomic fsynced write), if any."""
        if getattr(self, "_dirty", False) and self._store_path is not None:
            self.save()
            self._dirty = False

    @staticmethod
    def _format(kind: str, token: str) -> str:
        label = KINDS[kind]
        if kind == "email":
            return f"user-{token.lower()}@example.invalid"
        if kind == "domain":
            return f"domain-{token.lower()}.example.invalid"
        if kind == "phone":
            return f"+000-{token}"
        return f"{label}-{token}"

    def __len__(self) -> int:
        return len(self._map)

    def save(self) -> None:
        """Atomically persist the map (write temp file, fsync, rename)."""
        if self._store_path is None:
            raise IdentityError("no store path configured")
        data = {
            "version": _SCHEMA_VERSION,
            "secret_fingerprint": self._fingerprint(),
            "entries": dict(sorted(self._map.items())),
        }
        directory = self._store_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=self._store_path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._store_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:  # pragma: no cover - platform dependent
            pass

    def _load(self) -> None:
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IdentityError(f"cannot read pseudonym store ({type(exc).__name__})") from None
        if (
            not isinstance(data, dict)
            or data.get("version") != _SCHEMA_VERSION
            or not isinstance(data.get("entries"), dict)
        ):
            raise IdentityError("pseudonym store has unsupported format")
        if data.get("secret_fingerprint") != self._fingerprint():
            raise IdentityError("pseudonym store was created with a different secret")
        entries = data["entries"]
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items()):
            raise IdentityError("pseudonym store is malformed")
        self._map = dict(entries)
        self._used = set(entries.values())
