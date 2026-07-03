"""TBD-sentinel promotion — rewrites ``tbd("intent")`` calls in SUT source.

Called end-of-attempt in Step 9. After the runtime cache has been populated
by test execution, we scan the SUT for outstanding ``tbd()`` sentinels and
substitute each one with the resolved selector — provided the cache entry
passes both gates:

1. ``passing_witnesses`` non-empty — at least one passing test exercised
   this resolution (the runtime's ``pytest_runtest_teardown`` hook records
   this). Selectors only touched by failing tests never reach source.
2. ``validate_selector_payload`` — the discriminated-union check that
   rejects Playwright debug-print syntax, unbalanced brackets, injection
   markers, and structurally-malformed payloads.

Structured payloads emit runtime-helper calls (``role_locator(...)``,
``text_locator(...)``, etc.); the POM's ``from tests.qtea_runtime import``
line is extended in place to include the needed helpers. Cache entries
blocked by either gate become ``promotion-blocked`` bug-candidates so the
operator sees what's still chewing on the JIT runtime.
"""

from __future__ import annotations

import json
from pathlib import Path


def _format_promoted_substitution(payload: dict | None, selector: str | None) -> str | None:
    """Render a cache entry as the Python expression to substitute for `tbd(...)`.

    Returns the substitution string (a `json.dumps`'d CSS string OR a call
    like `role_locator("link", name="...")`), or None when the entry has no
    representable form. None means "leave the tbd() in place" — the caller
    emits a promotion-blocked bug-candidate.

    For structured payloads, we emit calls to the runtime helpers
    (`role_locator`, `text_locator`, …) defined in
    ``src/qtea/_resources/runtime/qtea_runtime.py.tpl``. The
    codegen-pom-extender ensures the runtime import is already present in
    the POM file (`from tests.qtea_runtime import tbd`); the new
    helpers live in the same module, so we may need to extend that import.
    """
    if isinstance(payload, dict):
        kind = payload.get("kind")
        if kind == "css":
            sel = payload.get("selector") or selector
            if not sel:
                return None
            return json.dumps(sel)
        if kind == "role":
            role = payload.get("role")
            if not role:
                return None
            parts = [f"role_locator({json.dumps(role)}"]
            if payload.get("name"):
                parts.append(f", name={json.dumps(payload['name'])}")
            if payload.get("exact") is True:
                parts.append(", exact=True")
            parts.append(")")
            return "".join(parts)
        if kind in ("text", "label", "placeholder"):
            text = payload.get("text")
            if not text:
                return None
            fn = f"{kind}_locator"
            parts = [f"{fn}({json.dumps(text)}"]
            if payload.get("exact") is True:
                parts.append(", exact=True")
            parts.append(")")
            return "".join(parts)
        if kind == "test_id":
            value = payload.get("value")
            if not value:
                return None
            return f"test_id_locator({json.dumps(value)})"
        return None
    # No payload — fall back to the legacy CSS-string path.
    if not selector:
        return None
    return json.dumps(selector)


def _ensure_runtime_imports(text: str, needed_names: set[str]) -> str:
    """Extend `from tests.qtea_runtime import …` to include `needed_names`.

    No-op when no such import line exists (caller's POM doesn't follow the
    convention; promotion still works for CSS-string substitutions because
    those don't need extra symbols).
    """
    import re as _re

    if not needed_names:
        return text
    pat = _re.compile(
        r"^(from\s+tests\.qtea_runtime\s+import\s+)([^\n]+)$",
        _re.MULTILINE,
    )
    m = pat.search(text)
    if not m:
        return text
    existing = {n.strip() for n in m.group(2).split(",") if n.strip()}
    missing = needed_names - existing
    if not missing:
        return text
    new_imports = ", ".join(sorted(existing | needed_names))
    return text[:m.start()] + m.group(1) + new_imports + text[m.end():]


def _promote_resolved_tbds(
    sut_root: Path, cache_path: Path,
) -> tuple[list[str], list[dict]]:
    """Replace tbd("intent") sentinels with their resolved selectors in-place.

    Returns ``(modified_files, blocked_candidates)``:
      - ``modified_files`` — SUT-relative paths of files actually rewritten.
      - ``blocked_candidates`` — bug-candidate dicts for entries the promoter
        REFUSED to substitute (no passing witness OR fails validation OR
        unrepresentable payload). The caller appends these to bug-candidates.json.

    Gating (the safety net added after the run-20260621 regression):
      1. ``passing_witnesses`` must be non-empty — the selector has been used
         by at least one test that PASSED in this attempt. Selectors that
         only failing tests touched never reach SUT source.
      2. ``validate_selector_payload(payload, selector)`` must return ok —
         catches Playwright debug-print syntax (`link "..."`), unbalanced
         brackets, injection markers, and structurally-malformed payloads.
      3. ``_format_promoted_substitution`` must yield a valid Python
         expression for the substitution. Structured payloads emit
         `role_locator(...)` / `text_locator(...)` / etc.; the runtime
         import line is extended to include the needed helpers.
    """
    import re as _re

    from qtea.jit_resolver import validate_selector_payload
    from qtea.tbd_scanner import scan_tbd_intents

    blocked: list[dict] = []
    if not cache_path.exists():
        return [], blocked
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], blocked

    intent_to_entry: dict[str, dict] = {}
    for e in (data.get("entries") or []):
        intent = e.get("intent")
        if not intent:
            continue
        if e.get("source", "none") == "none":
            continue
        # Gate 1: passing-test witness required. The cache file may carry
        # entries with no witnesses yet (resolved + used in a failing test);
        # those stay as tbd() this round but may earn witnesses on a later run.
        witnesses = e.get("passing_witnesses")
        if not isinstance(witnesses, list) or not witnesses:
            blocked.append({
                "id": f"BC-promotion-blocked-{e.get('constant_name', 'unknown')}",
                "kind": "promotion-blocked",
                "reason": "no_passing_witness",
                "intent": intent,
                "constant_name": e.get("constant_name"),
                "cached_selector": e.get("selector"),
                "cached_payload": e.get("payload"),
                "remediation": (
                    "No passing test has used this resolution yet. The "
                    "selector stays as tbd() so the JIT runtime keeps "
                    "resolving it. Add a test that exercises it OR fix the "
                    "test that triggered the resolution."
                ),
            })
            continue
        # Gate 2: structural validation.
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else None
        ok, why = validate_selector_payload(payload, e.get("selector"))
        if not ok:
            blocked.append({
                "id": f"BC-promotion-blocked-{e.get('constant_name', 'unknown')}",
                "kind": "promotion-blocked",
                "reason": "invalid_selector_form",
                "intent": intent,
                "constant_name": e.get("constant_name"),
                "cached_selector": e.get("selector"),
                "cached_payload": payload,
                "validation_reason": why,
                "remediation": (
                    "The cached selector failed validate_selector_payload. "
                    "Drop the bad cache entry (rm locator-cache.json) and "
                    "re-run; the resolver will try again with the updated "
                    "prompt that demands structured payloads."
                ),
            })
            continue
        intent_to_entry[intent] = e

    if not intent_to_entry:
        return [], blocked

    scan_roots = [p for p in (sut_root / d for d in ("src", "tests", "pages")) if p.is_dir()]
    if not scan_roots:
        scan_roots = [sut_root]
    hits = scan_tbd_intents(scan_roots, sut_root=sut_root)

    by_file: dict[Path, list] = {}
    for hit in hits:
        if hit.intent in intent_to_entry:
            by_file.setdefault(hit.file, []).append(hit)

    # Map runtime helper kinds to import names — added to the POM's
    # `from tests.qtea_runtime import ...` line when the substitution
    # uses them. CSS / no-payload substitutions don't need any extra imports.
    _KIND_TO_HELPER = {
        "role": "role_locator",
        "text": "text_locator",
        "label": "label_locator",
        "placeholder": "placeholder_locator",
        "test_id": "test_id_locator",
    }

    modified: list[str] = []
    for file_path, file_hits in by_file.items():
        abs_path = (sut_root / file_path) if not file_path.is_absolute() else file_path
        rel_str = str(file_path) if not file_path.is_absolute() else str(file_path.relative_to(sut_root))
        text = abs_path.read_text(encoding="utf-8")
        new_text = text
        helper_imports: set[str] = set()
        for hit in file_hits:
            entry = intent_to_entry[hit.intent]
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
            substitution = _format_promoted_substitution(payload, entry.get("selector"))
            if substitution is None:
                blocked.append({
                    "id": f"BC-promotion-blocked-{entry.get('constant_name', 'unknown')}",
                    "kind": "promotion-blocked",
                    "reason": "unrepresentable_payload",
                    "intent": hit.intent,
                    "constant_name": entry.get("constant_name"),
                    "cached_selector": entry.get("selector"),
                    "cached_payload": payload,
                    "remediation": "Payload could not be rendered; investigate jit_resolver / cache state.",
                })
                continue
            if isinstance(payload, dict):
                helper = _KIND_TO_HELPER.get(payload.get("kind"))
                if helper:
                    helper_imports.add(helper)
            escaped = _re.escape(hit.intent)
            new_text = _re.sub(
                rf'tbd\((?P<q>["\']){escaped}(?P=q)\)',
                lambda _m, _r=substitution: _r,
                new_text,
            )
        # Add any new helper imports BEFORE writing back.
        if helper_imports:
            new_text = _ensure_runtime_imports(new_text, helper_imports)
        if new_text != text:
            abs_path.write_text(new_text, encoding="utf-8")
            modified.append(rel_str)
    return modified, blocked


__all__ = [
    "_ensure_runtime_imports",
    "_format_promoted_substitution",
    "_promote_resolved_tbds",
]
