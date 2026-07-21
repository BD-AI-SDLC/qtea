"""JIT locator-cache dev-pool prewarm + resolver-spend summarizer.

- ``_prewarm_jit_cache_dev_pool`` scans the SUT for outstanding ``tbd(...)``
  intents and pre-populates the runtime cache with tier-1b dev-pool matches
  so the first test that hits each intent skips the fuzzy lookup entirely
  (saves ~10ms per hit on a warm dev-pool run).
- ``_summarize_resolver_spend`` folds ``resolver-spend.jsonl`` into a compact
  telemetry block for ``run-results.json`` — counts, tier hits, and rough
  cost estimation. Deliberately excludes selectors / URLs / snapshot bodies
  so the summary stays cheap to log and free of PII.

Inline imports (``from qtea import jit_resolver`` etc.) MUST stay inline —
hoisting them would cycle back through the pipeline module during import.
"""

from __future__ import annotations

import json
from pathlib import Path

from qtea.steps.base import StepContext


def _derive_scan_roots(ctx: StepContext, sut_root: Path) -> list[Path]:
    """Directories to scan for ``tbd()`` sentinels.

    Starts from the conventional POM roots (``src``/``tests``/``pages``) and
    adds inventory-derived roots so non-POM layouts (e.g. Screenplay under
    ``framework/``) are covered — the active module's ``package_root``, the
    first path segment of each captured ``pattern_exemplars[].dir``, and the
    test ``base_dir``. Without this, a Screenplay SUT's deferred-Target
    ``tbd()`` calls under ``framework/`` are silently never prewarmed.
    Falls back to the whole SUT when nothing resolves.
    """
    rel_dirs: set[str] = {"src", "tests", "pages"}
    try:
        research_path = ctx.workspace.step_dir(6) / "research.json"
        if research_path.is_file():
            research = json.loads(research_path.read_text(encoding="utf-8"))
            inv = research.get("sut_inventory") or {}
            active = inv.get("active_module")
            for mod in inv.get("modules") or []:
                if not isinstance(mod, dict) or mod.get("name") != active:
                    continue
                src_layout = mod.get("src_directory_layout") or {}
                pkg = src_layout.get("package_root")
                if isinstance(pkg, str) and pkg:
                    rel_dirs.add(pkg.replace("\\", "/").strip("/").split("/")[0])
                for ex in mod.get("pattern_exemplars") or []:
                    d = (ex.get("dir") or "").replace("\\", "/").strip("/")
                    if d and d != ".":
                        rel_dirs.add(d.split("/")[0])
                test_layout = mod.get("test_directory_layout") or {}
                base = test_layout.get("base_dir")
                if isinstance(base, str) and base:
                    rel_dirs.add(base.replace("\\", "/").strip("/").split("/")[0])
                break
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    roots = [sut_root / d for d in sorted(rel_dirs) if (sut_root / d).is_dir()]
    return roots or [sut_root]


def _prewarm_jit_cache_dev_pool(
    *,
    ctx: StepContext,
    jit_cache_dir: Path,
    dev_locators_path: Path,
) -> int:
    """Pre-populate the locator cache with tier-1b dev-pool matches for
    every TBD sentinel currently in the SUT source tree. Returns count.

    Source of truth: the live SUT — scanned via
    :func:`qtea.tbd_scanner.scan_tbd_intents`. That guarantees we
    prewarm the intents the next test run will actually request, even
    if step 8's archived ``tbd-index.json`` has gone stale (heal
    agent rewrote a constant, manual edit between steps, etc.).
    No-op when no TBDs are present or no dev-locator pool is supplied.
    """
    from qtea import jit_resolver
    from qtea.runtime.dev_locators import load_dev_locators
    from qtea.tbd_scanner import scan_tbd_intents

    sut_root = ctx.workspace.sut
    scan_roots = _derive_scan_roots(ctx, sut_root)
    hits = scan_tbd_intents(scan_roots, sut_root=sut_root)
    if not hits:
        return 0

    # Dedupe by (intent, constant_name) — same intent referenced by
    # multiple constants or files still needs one prewarm per cache key,
    # which the resolver will dedupe internally via cache_key().
    intents_payload: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for h in hits:
        intent = (h.intent or "").strip()
        const = (h.constant_name or "").strip() or intent  # bare tbd() → intent as const
        dedupe_key = (intent, h.constant_name)
        if not intent or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        intents_payload.append({
            "intent": intent,
            "constant_name": const,
            "test_file": str(h.file).replace("\\", "/"),
        })

    if not intents_payload:
        return 0

    locators, _src, _warnings = load_dev_locators(cli_path=dev_locators_path)
    if not locators:
        return 0
    return jit_resolver.prewarm_dev_pool_cache(
        tbd_intents=intents_payload,
        dev_locators=locators,
        cache_path=jit_cache_dir / "locator-cache.json",
        run_id=ctx.workspace.run_id,
    )


def _summarize_resolver_spend(jit_cache_dir: Path) -> dict | None:
    """Read ``<jit_cache_dir>/resolver-spend.jsonl`` and build a summary
    block for ``run-results.json``. Returns None when no spend file was
    produced (no JIT runtime ran, or no resolution events fired).

    Telemetry shape kept narrow on purpose — counts, totals, and hits per
    tier. No selectors, page URLs, or snapshot bodies (privacy + size).
    """
    p = jit_cache_dir / "resolver-spend.jsonl"
    if not p.is_file():
        return None
    tier_hits = {1: 0, 2: 0, 3: 0, 4: 0}
    total_input = 0
    total_output = 0
    unresolvable = 0
    fallback_promoted_count = 0
    durations_ms: list[int] = []
    models: set[str] = set()
    count = 0
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                count += 1
                tier = entry.get("tier")
                if tier in tier_hits:
                    tier_hits[tier] += 1
                total_input += int(entry.get("input_tokens") or 0)
                total_output += int(entry.get("output_tokens") or 0)
                if entry.get("model"):
                    models.add(entry["model"])
                if entry.get("duration_ms") is not None:
                    durations_ms.append(int(entry["duration_ms"]))
                if entry.get("success") is False:
                    unresolvable += 1
                if entry.get("fallback_promoted"):
                    fallback_promoted_count += 1
    except OSError:
        return None
    if count == 0:
        return None
    # Cost estimation reuses the existing pricing table if available;
    # otherwise the consumer can compute it from input/output tokens.
    est_cost_usd: float | None = None
    try:
        from qtea.llm.cost import estimate_cost  # type: ignore[import-not-found]
        for m in (models or {""}):
            est_cost_usd = (est_cost_usd or 0.0) + estimate_cost(
                m, total_input, total_output,
            )
    except Exception:
        est_cost_usd = None
    return {
        "total_resolutions": count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "tier_1_hits": tier_hits[1],
        "tier_2_hits": tier_hits[2],
        "tier_3_hits": tier_hits[3],
        "tier_4_hits": tier_hits[4],
        "unresolvable_count": unresolvable,
        "fallback_promoted_count": fallback_promoted_count,
        "models": sorted(models) or None,
        "median_duration_ms": (
            sorted(durations_ms)[len(durations_ms) // 2] if durations_ms else None
        ),
        "est_cost_usd": est_cost_usd,
    }


__all__ = [
    "_prewarm_jit_cache_dev_pool",
    "_summarize_resolver_spend",
]
