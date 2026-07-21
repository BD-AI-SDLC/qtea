"""Cross-run incident memory: persist debug/fix-proposal diagnoses per-SUT
and retrieve similar past incidents before a new investigation starts.

Storage root: ``~/.qtea/incident-memory/<sut_fingerprint>/<incident_id>.json``
(overridable via ``QTEA_INCIDENT_MEMORY_DIR``). One file per incident, written
atomically (tmp + rename) — safe under concurrent ``qtea run`` processes
because every filename is unique; no process ever needs to read-modify-write a
shared file. This is a NEW allowed filesystem root beyond ``<sut>/`` and
``<workspace>/`` (CLAUDE.md § Hard Rules "Filesystem containment").

v1 scope: only the debug/fix-proposal chain (any step 1-11 that exhausts its
retry budget) feeds this store. Step 9's self-heal loop is a v2 extension —
that's also where ``resolved`` / ``outcome_notes`` would start being populated
with *verified* (not merely proposed) fixes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from qtea._sut_git import is_git_url
from qtea.config import get_settings
from qtea.failure_classifiers import FailureCategory
from qtea.logging_setup import get_logger
from qtea.redaction import redact_text
from qtea.schemas import is_valid

if TYPE_CHECKING:
    from qtea.steps.base import StepContext

log = get_logger(__name__)

SCHEMA_NAME = "incident-record"
SCHEMA_VERSION = 1
_EXCERPT_CAP = 4000
_SIGNATURE_CAP = 500
# Retrieval scoring weights.
_TAG_WEIGHT = 0.6
_SIG_WEIGHT = 0.4
# Common English/log-noise tokens that carry no discriminative signal.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "not", "was", "were",
    "error", "failed", "failure", "step", "attempt", "traceback", "line",
    "file", "none", "true", "false", "self", "return", "raise",
})


def incident_memory_root() -> Path:
    """Root directory for the cross-run incident store."""
    return get_settings().incident_memory_dir


def incident_memory_enabled(ctx: "StepContext") -> bool:
    """Whether incident memory read+write is active for this run.

    Mirrors the ``--no-fix`` / ``QTEA_NO_STATIC_CHECK`` dual-opt-out pattern:
    the CLI options field is checked first, then the env var, so either side
    suppresses both retrieval and persistence.
    """
    if getattr(ctx.options, "no_incident_memory", False):
        return False
    return os.environ.get("QTEA_NO_INCIDENT_MEMORY") != "1"


def sut_fingerprint(sut_source: str) -> str:
    """Stable per-SUT key derived from the raw ``--sut`` value.

    IMPORTANT: pass ``ctx.sut_source`` (the raw CLI value), NOT the
    materialized ``ctx.workspace.sut`` clone — Step 6 strips the ``origin``
    remote from git clones (s06_research._materialize_sut) so the remote is
    unreadable from the working copy by the time a later step fails.

    Git URL  -> normalized URL (drop scheme creds/query, strip trailing
                ``.git``, lowercase host), sha256[:16].
    Local path -> resolved absolute path, sha256[:16].
    Empty/unknown -> ``"unknown"``.
    """
    s = (sut_source or "").strip()
    if not s:
        return "unknown"
    if is_git_url(s):
        norm = _normalize_git_url(s)
    else:
        try:
            norm = str(Path(s).expanduser().resolve())
        except (OSError, RuntimeError):
            norm = s
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _normalize_git_url(url: str) -> str:
    """Best-effort canonicalization so ``repo.git`` and ``repo`` collapse."""
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    u = u.rstrip("/")
    # scp-style (git@host:org/repo) has no scheme urlparse understands.
    if u.startswith(("http://", "https://", "ssh://")):
        try:
            parsed = urlparse(u)
            host = parsed.hostname or ""
            path = parsed.path
            return f"{host.lower()}{path}"
        except Exception:
            return u.lower()
    return u.lower()


@dataclass
class IncidentRecord:
    incident_id: str
    schema_version: int
    sut_fingerprint: str
    recorded_at: str
    run_id: str
    step_num: int
    step_name: str
    attempt: int
    category: str
    symptom_tags: list[str]
    failure_signature: str
    failure_context_excerpt: str
    rca_excerpt: str
    fix_strategy_excerpt: str
    fix_proposal_path: str | None
    resolved: bool = False
    outcome_notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncidentRecord":
        fields = {
            "incident_id", "schema_version", "sut_fingerprint", "recorded_at",
            "run_id", "step_num", "step_name", "attempt", "category",
            "symptom_tags", "failure_signature", "failure_context_excerpt",
            "rca_excerpt", "fix_strategy_excerpt", "fix_proposal_path",
            "resolved", "outcome_notes",
        }
        return cls(**{k: v for k, v in data.items() if k in fields})


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens (>=3 chars, minus stopwords) for tag matching."""
    toks = re.findall(r"[a-z0-9_./-]{3,}", (text or "").lower())
    return {t for t in toks if t not in _STOPWORDS}


def _cap(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """tmp + rename with a per-process-unique tmp name.

    Unlike checkpoints.py / jit_resolver.py (fixed ``.tmp`` suffix on a
    per-workspace file), the incident store is shared across concurrent
    ``qtea run`` processes, so the tmp name is PID+uuid-suffixed to avoid
    two processes racing on the same tmp path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(path)


def record_incident(
    *,
    ctx: "StepContext",
    step_num: int,
    step_name: str,
    attempt: int,
    category: FailureCategory,
    failure_context: str,
    debug_rca_text: str,
    strategy_text: str,
    fix_proposal_path: Path | None,
) -> Path | None:
    """Persist one diagnosed incident. Never raises.

    Returns the written path, ``None`` when disabled or on any error (logged).
    """
    if not incident_memory_enabled(ctx):
        return None
    try:
        fingerprint = sut_fingerprint(ctx.sut_source or "")
        signature = _cap(
            redact_text((failure_context or "").strip().splitlines()[0])
            if (failure_context or "").strip()
            else f"step {step_num} {step_name} failure",
            _SIGNATURE_CAP,
        )
        tags = sorted(
            _tokenize(signature)
            | _tokenize(category.value)
            | _tokenize(step_name)
        ) or [category.value]

        proposal_str: str | None = None
        if fix_proposal_path is not None:
            try:
                proposal_str = str(
                    fix_proposal_path.relative_to(ctx.workspace.root)
                )
            except (ValueError, AttributeError):
                proposal_str = str(fix_proposal_path)

        record = IncidentRecord(
            incident_id=uuid.uuid4().hex,
            schema_version=SCHEMA_VERSION,
            sut_fingerprint=fingerprint,
            recorded_at=datetime.now(UTC).isoformat(),
            run_id=getattr(ctx.state, "run_id", ""),
            step_num=step_num,
            step_name=step_name,
            attempt=attempt,
            category=category.value,
            symptom_tags=tags,
            failure_signature=signature,
            failure_context_excerpt=_cap(redact_text(failure_context), _EXCERPT_CAP),
            rca_excerpt=_cap(redact_text(debug_rca_text), _EXCERPT_CAP),
            fix_strategy_excerpt=_cap(redact_text(strategy_text), _EXCERPT_CAP),
            fix_proposal_path=proposal_str,
            resolved=False,
            outcome_notes=None,
        )
        payload = record.to_dict()
        ok, err = is_valid(payload, SCHEMA_NAME)
        if not ok:
            log.warning("incident_memory.schema_invalid", step=step_num, error=err)
            return None

        out_path = (
            incident_memory_root() / fingerprint / f"{record.incident_id}.json"
        )
        _atomic_write_json(out_path, payload)
        log.info(
            "incident_memory.recorded",
            step=step_num,
            fingerprint=fingerprint,
            path=str(out_path),
        )
        return out_path
    except Exception as e:  # noqa: BLE001 — persistence must never abort pipeline
        log.warning("incident_memory.record_failed", step=step_num, error=str(e))
        return None


def _score(query_tags: set[str], query_sig: str, rec: IncidentRecord) -> float:
    rec_tags = set(rec.symptom_tags)
    if query_tags or rec_tags:
        union = query_tags | rec_tags
        jaccard = len(query_tags & rec_tags) / len(union) if union else 0.0
    else:
        jaccard = 0.0
    sig_ratio = SequenceMatcher(
        None, query_sig.lower(), (rec.failure_signature or "").lower()
    ).ratio()
    return _TAG_WEIGHT * jaccard + _SIG_WEIGHT * sig_ratio


def query_similar(
    *,
    fingerprint: str,
    step_num: int,
    failure_signature: str,
    limit: int = 5,
) -> list[IncidentRecord]:
    """Return up to ``limit`` past incidents for THIS SUT + step, most similar
    first. Never raises — corrupt/unreadable files are skipped with a warning.
    """
    try:
        sut_dir = incident_memory_root() / fingerprint
        if not sut_dir.is_dir():
            return []
        query_tags = _tokenize(failure_signature) | _tokenize(str(step_num))
        query_sig = redact_text(failure_signature or "")
        scored: list[tuple[float, IncidentRecord]] = []
        for f in sut_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                rec = IncidentRecord.from_dict(data)
            except (OSError, ValueError, TypeError) as e:
                log.warning("incident_memory.read_skip", path=str(f), error=str(e))
                continue
            if rec.step_num != step_num:
                continue
            scored.append((_score(query_tags, query_sig, rec), rec))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [rec for score, rec in scored[:limit] if score > 0]
    except Exception as e:  # noqa: BLE001 — retrieval must never abort debug
        log.warning("incident_memory.query_failed", error=str(e))
        return []


def render_prior_incidents_md(records: list[IncidentRecord]) -> str:
    """Render a shortlist of past incidents as a markdown briefing for the
    debug agent. The excerpts are self-contained (do NOT rely on
    fix_proposal_path being dereferenceable in a later run's workspace).
    """
    if not records:
        return ""
    lines = [
        "# Prior Incidents on This SUT",
        "",
        "The following past incidents on THIS system-under-test may share a "
        "root cause with the current failure. Treat them as LEADS to confirm "
        "or rule out — your own Read/Grep/Glob investigation is authoritative, "
        "not this list.",
        "",
    ]
    for i, rec in enumerate(records, 1):
        lines += [
            f"## Incident {i} — {rec.category} (step {rec.step_num}, {rec.recorded_at})",
            "",
            f"**Failure signature:** {rec.failure_signature}",
            "",
            "**Root-cause analysis (excerpt):**",
            "",
            rec.rca_excerpt.strip() or "_(none recorded)_",
            "",
            "**Fix strategy (excerpt):**",
            "",
            rec.fix_strategy_excerpt.strip() or "_(none recorded)_",
            "",
            "---",
            "",
        ]
    return "\n".join(lines)
