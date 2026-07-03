"""Debug-folder wipe on resume — unit tests.

Covers the invariant "when a step is resumed, `<workspace>/debug/` is empty".
The prior implementation only globbed `step-NN-attempt*` and left aggregated
RCA / fix-proposal / fix-workdir entries in place, so the next debug pass
saw stale files from prior sessions. See run 20260701-114656-9394eb.

Tests hit ``_cleanup_step_artifacts`` directly (auto_confirm=True to skip
the interactive prompt) with a workspace seeded with every debug-file shape
qtea can produce.
"""

from __future__ import annotations

from pathlib import Path

from qtea.pipeline import _cleanup_step_artifacts
from qtea.workspace import create_workspace


def _seed_debug_layout(debug_dir: Path) -> list[Path]:
    """Create every kind of debug artefact qtea can drop on the ground and
    return the list of paths for later assertion."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    # Per-attempt RCA (the ONLY shape the old glob caught)
    p = debug_dir / "step-05-attempt1-debug-rca.md"
    p.write_text("attempt-1 rca", encoding="utf-8")
    paths.append(p)

    # Per-attempt RCA workdir
    d = debug_dir / "step-05-attempt2-rca"
    d.mkdir()
    (d / "debug-rca.md").write_text("scratchpad", encoding="utf-8")
    paths.append(d)

    # Aggregated RCA
    p = debug_dir / "step-05-rca.md"
    p.write_text("aggregated rca", encoding="utf-8")
    paths.append(p)

    # Aggregated fix-proposal
    p = debug_dir / "step-05-fix-proposal.md"
    p.write_text("proposal", encoding="utf-8")
    paths.append(p)

    # Fix-workdir with thinking / eng sub-dirs
    d = debug_dir / "step-05-fix"
    (d / "thinking").mkdir(parents=True)
    (d / "eng").mkdir(parents=True)
    (d / "eng" / "fix-proposal.md").write_text("eng out", encoding="utf-8")
    paths.append(d)

    # Legacy stray file with no per-step naming
    p = debug_dir / "leftover-notes.md"
    p.write_text("some prior operator's scratch", encoding="utf-8")
    paths.append(p)

    return paths


def test_resume_wipes_all_debug_entries_for_target_step(tmp_path: Path):
    """When resuming from step 5, every debug artefact for step 5 must be
    removed — not just `step-05-attempt*`. Also, the stray `leftover-notes.md`
    with no per-step naming must be swept by the residual sweep."""
    ws = create_workspace(tmp_path)
    seeded = _seed_debug_layout(ws.debug)
    for p in seeded:
        assert p.exists()

    _cleanup_step_artifacts(ws, from_step=5, auto_confirm=True)

    for p in seeded:
        assert not p.exists(), f"debug entry survived cleanup: {p.name}"
    assert list(ws.debug.iterdir()) == []


def test_resume_wipes_debug_when_resuming_from_step_1(tmp_path: Path):
    """Resuming from step 1 must empty the entire debug folder regardless of
    which step's failure produced the artefacts."""
    ws = create_workspace(tmp_path)
    (ws.debug / "step-03-rca.md").parent.mkdir(parents=True, exist_ok=True)
    (ws.debug / "step-03-rca.md").write_text("s3 rca", encoding="utf-8")
    (ws.debug / "step-09-rca.md").write_text("s9 rca", encoding="utf-8")
    (ws.debug / "step-09-fix-proposal.md").write_text("s9 proposal", encoding="utf-8")

    _cleanup_step_artifacts(ws, from_step=1, auto_confirm=True)

    assert list(ws.debug.iterdir()) == []


def test_resume_leaves_debug_untouched_when_from_step_beyond_all(tmp_path: Path):
    """Sanity: cleanup does not raise on a workspace with no debug entries.

    Note: the residual-sweep will still empty `debug/` if it exists. The
    contract is "empty on resume" per the user requirement, so an empty
    folder is the correct steady state — even when `from_step > TOTAL_STEPS`
    (a caller-error case that shouldn't happen in practice)."""
    ws = create_workspace(tmp_path)
    # No debug artefacts seeded.
    _cleanup_step_artifacts(ws, from_step=12, auto_confirm=True)
    # No error raised is the invariant here.
