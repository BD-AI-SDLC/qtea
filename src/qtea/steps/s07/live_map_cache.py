"""Cross-run cache for Step 7's ``live-map.json``.

Keyed on inputs that actually shape the map:

  * SUT git SHA (or a content hash when the SUT isn't a git repo)
  * test-design.md SHA256
  * SUT base URL
  * auth-prewarm mode
  * probe-version constant (bump when ``_DOM_PROBE_JS`` changes)

Before a cache hit is honored, a **liveness probe** hits the base URL and
compares its response fingerprint (ETag / Last-Modified / body SHA) to the
fingerprint stored alongside the cache entry. This guards deployed SUTs whose
git SHA never changes but whose content does.

Two toggles:

  * ``QTEA_LIVE_MAP_CACHE=off`` — bypass the cache entirely.
  * ``QTEA_LIVE_MAP_CACHE_DIR=<dir>`` — override the cache location.

Cache location default: ``~/.qtea/live-map-cache/`` (workspace-independent so
a clean workspace re-run still benefits). Storage-state files are NEVER cached
here — they hold credentials and have their own lifecycle in
:mod:`qtea.storage_state`.

Best-effort: any error (unreadable cache file, network error on liveness probe,
missing git) returns ``None`` and the caller re-explores. Never raises.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from qtea.logging_setup import get_logger

log = get_logger(__name__)


# Bump when `_DOM_PROBE_JS` changes shape in a way that invalidates prior
# live-maps (element schema change, locator field additions). A monotonic
# integer is enough; keep this in sync with ``s07_live_explore._DOM_PROBE_JS``
# revisions.
PROBE_VERSION = 1

# Where cache entries live. Under the user home so a workspace wipe doesn't
# discard them. Override with QTEA_LIVE_MAP_CACHE_DIR.
_DEFAULT_CACHE_ROOT = Path.home() / ".qtea" / "live-map-cache"

# Cap on how long a liveness probe may run before we give up and re-explore.
_LIVENESS_TIMEOUT_S = 5


@dataclass
class CacheKey:
    """Composed cache key. ``fingerprint`` is a stable SHA256 hex string."""

    sut_hash: str
    design_hash: str
    base_url: str
    auth_mode: str
    probe_version: int

    @property
    def fingerprint(self) -> str:
        payload = "\x1f".join([
            f"probe={self.probe_version}",
            f"sut={self.sut_hash}",
            f"design={self.design_hash}",
            f"base_url={self.base_url}",
            f"auth_mode={self.auth_mode}",
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_enabled() -> bool:
    """False when QTEA_LIVE_MAP_CACHE=off, else True."""
    return (os.environ.get("QTEA_LIVE_MAP_CACHE", "") or "").strip().lower() != "off"


def cache_root() -> Path:
    """Resolve the cache directory, honoring the env override."""
    override = os.environ.get("QTEA_LIVE_MAP_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_CACHE_ROOT


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sut_git_sha(sut_root: Path) -> str | None:
    """HEAD SHA of the SUT repo, or ``None`` when the SUT isn't a git repo."""
    try:
        res = subprocess.run(
            ["git", "-C", str(sut_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode != 0:
            return None
        sha = (res.stdout or "").strip()
        return sha if sha else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _sut_content_hash(sut_root: Path) -> str:
    """Fallback content hash for non-git SUTs: hash the manifest of
    ``package.json``/``pyproject.toml``/``requirements.txt`` at the SUT root
    (whichever exist), plus their mtimes. Cheap and stable across quick reruns.
    """
    parts: list[str] = []
    for name in ("package.json", "pyproject.toml", "requirements.txt"):
        p = sut_root / name
        if p.is_file():
            try:
                st = p.stat()
                parts.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
            except OSError:
                continue
    return _sha256_text("\n".join(parts) or f"sut_root:{sut_root}")


def compute_key(
    *,
    sut_root: Path,
    test_design_text: str,
    base_url: str,
    auth_mode: str,
) -> CacheKey:
    """Build a CacheKey from the current inputs."""
    sha = _sut_git_sha(sut_root)
    sut_hash = sha or _sut_content_hash(sut_root)
    design_hash = _sha256_text(test_design_text or "")
    return CacheKey(
        sut_hash=sut_hash,
        design_hash=design_hash,
        base_url=(base_url or "").rstrip("/"),
        auth_mode=(auth_mode or "").lower() or "unknown",
        probe_version=PROBE_VERSION,
    )


def _entry_path(key: CacheKey) -> Path:
    return cache_root() / f"{key.fingerprint}.json"


def _liveness_fingerprint(base_url: str) -> str | None:
    """Cheap fingerprint of the SUT's landing page to detect content drift.

    Prefers ETag / Last-Modified response headers; falls back to a short body
    SHA. Returns ``None`` on any error (caller then can't validate liveness and
    re-explores).
    """
    if not base_url:
        return None
    try:
        # Lazy import — cache module shouldn't pull urllib during import.
        from urllib.error import URLError
        from urllib.request import Request, urlopen
    except Exception:
        return None
    try:
        req = Request(base_url, headers={"User-Agent": "qtea-live-map-cache/1"})
        with urlopen(req, timeout=_LIVENESS_TIMEOUT_S) as resp:  # noqa: S310
            etag = resp.headers.get("ETag") or ""
            last_mod = resp.headers.get("Last-Modified") or ""
            if etag or last_mod:
                return _sha256_text(f"etag={etag}\x1flast_mod={last_mod}")
            # Read a bounded prefix of the body (SPAs serve a small index.html).
            body = resp.read(32 * 1024)
            return _sha256_bytes(body)
    except (URLError, OSError, ValueError) as e:
        log.info("step07.cache.liveness_error", base_url=base_url, error=str(e))
        return None


def load(key: CacheKey, *, verify_liveness: bool = True) -> dict[str, Any] | None:
    """Return the cached live-map for ``key``, or ``None`` on miss / stale /
    unreadable / disabled.

    When ``verify_liveness`` is true (default), a fresh liveness fingerprint is
    computed from the base_url and compared against the stored value. Mismatch
    invalidates the entry (returned as ``None``). Set ``verify_liveness=False``
    only in tests.
    """
    if not cache_enabled():
        return None
    path = _entry_path(key)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        log.info("step07.cache.unreadable", path=str(path), error=str(e))
        return None
    if not isinstance(entry, dict):
        return None
    stored_key = entry.get("key")
    if not isinstance(stored_key, dict) or stored_key.get("fingerprint") != key.fingerprint:
        # Fingerprint collision guard (shouldn't happen with SHA256 but cheap).
        return None
    live_map = entry.get("live_map")
    if not isinstance(live_map, dict):
        return None
    if verify_liveness:
        current_fp = _liveness_fingerprint(key.base_url)
        stored_fp = entry.get("liveness_fingerprint")
        if not current_fp or current_fp != stored_fp:
            log.info(
                "step07.cache.liveness_mismatch",
                path=str(path),
                base_url=key.base_url,
                stored=bool(stored_fp),
            )
            return None
    log.info("step07.cache.hit", path=str(path), key=key.fingerprint)
    return live_map


def save(key: CacheKey, live_map: dict[str, Any]) -> Path | None:
    """Persist ``live_map`` under ``key``. Returns the file path on success or
    ``None`` on any I/O error / disabled cache.
    """
    if not cache_enabled():
        return None
    root = cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.info("step07.cache.mkdir_error", root=str(root), error=str(e))
        return None
    path = _entry_path(key)
    entry = {
        "key": {
            "fingerprint": key.fingerprint,
            "sut_hash": key.sut_hash,
            "design_hash": key.design_hash,
            "base_url": key.base_url,
            "auth_mode": key.auth_mode,
            "probe_version": key.probe_version,
        },
        "liveness_fingerprint": _liveness_fingerprint(key.base_url),
        "saved_at": int(time.time()),
        "live_map": live_map,
    }
    try:
        path.write_text(
            json.dumps(entry, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        log.info("step07.cache.write_error", path=str(path), error=str(e))
        return None
    log.info("step07.cache.saved", path=str(path), key=key.fingerprint)
    return path
