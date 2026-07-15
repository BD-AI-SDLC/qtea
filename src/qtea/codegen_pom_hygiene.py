"""Structural hygiene checks for agent-authored POM methods and their test
call sites.

Two independent gates, both hard-fail Step 8 when they find violations:

- ``find_pom_assertion_violations`` — RCA-D structural check. The
  pom-assertion rule in ``test_indexer.py`` already flags ``expect(``
  inside POM bodies, but that check runs at Phase C (via ``index_tests``)
  which is downstream of Phase B.5 reconciliation. When B.5 hard-fails
  (as it did on run 20260708-121117-99f5ed), the pom-assertion check
  never runs on the broken-but-shipped POM. Running the same regex
  battery at Phase A3.5 catches drift the moment the extender writes it.

- ``find_return_consumption_violations`` — a class of drift the existing
  rules do not catch: the pom-extender promoted a plan-declared
  ``Promise<void>`` signature to ``Promise<{a, b}>`` (returning probe
  data), and the test-writer emitted ``await pom.foo();`` — discarding
  the return. The label the plan required to be asserted was READ from
  the DOM but never COMPARED to the expected literal. This gate scans
  every agent-authored POM method's EMITTED return type; when non-void,
  every call site in the generated tests must consume the return value
  (assign, destructure, wrap in ``expect(...)``, ``return``, ``throw``,
  or pass as a sub-expression).

Both gates apply ONLY to methods qtea just authored — pre-existing SUT
code is out of scope. The caller passes ``agent_authored_methods`` (the
union of ``code-modification-plan.missing_methods[*].name`` across all
POM tasks) so this module never has to open the plan itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qtea.codegen_body_verify import (
    _find_balanced_brace,
    _find_method_body_open,
    _java_method_bodies,
    _js_method_bodies,
)
from qtea.codegen_reconcile import (
    _JAVA_LIFECYCLE_NAMES,
    _JAVA_METHOD_HEAD_RE,
    _JS_CALL_HEAD_RE,
    _JS_IMPORT_RE,
    _JS_METHOD_HEAD_RE,
    _JS_NEW_RE,
    _LIFECYCLE_NAMES,
    _find_balanced,
    _js_class_body,
    _js_strip,
)
from qtea.logging_setup import get_logger
from qtea.test_indexer import _POM_ASSERTION_PATTERNS

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class HygieneViolation:
    """One breach of a structural hygiene rule."""

    rule: str  # "pom-assertion" | "return-consumption"
    file: str  # SUT-relative or absolute path
    line: int
    method: str
    message: str

    def format(self) -> str:
        return f"{self.file}:{self.line}: [{self.rule}] {self.method}: {self.message}"


# ---------------------------------------------------------------------------
# Fix 2 — pom-assertion structural gate (Phase A3.5)
# ---------------------------------------------------------------------------


def find_pom_assertion_violations(
    pom_file: Path,
    class_name: str,
    agent_authored_methods: set[str],
    *,
    language: str,
) -> list[HygieneViolation]:
    """Flag any ``expect(`` / ``assert`` / ``assertThat(`` / ``.should(``
    inside the body of a POM method the pom-extender just wrote.

    Reuses the regex battery from ``test_indexer._POM_ASSERTION_PATTERNS``
    so both gates catch the same shapes. The body-extraction helper
    (``_js_method_bodies`` for TS/JS, ``_java_method_bodies`` for Java)
    neutralises string literals and comments up-front via ``_js_strip``,
    so a comment like ``// use expect(...)`` won't false-positive.

    Python is not wired here — it has no equivalent early (Phase A3.5)
    catch and instead relies solely on ``test_indexer._scan_pom_assertions``
    at Phase C, which still hard-fails agent-authored violations after one
    self-fix attempt but can miss a POM left on disk by an earlier hard
    abort (see module docstring). Extending Python is a separate, larger
    change deferred until it's actually needed.
    """
    lang = (language or "").lower()
    is_java = lang in ("java", "selenium-java", "playwright-java")
    is_jsts = lang in ("typescript", "javascript")
    if not (is_jsts or is_java):
        return []
    if not agent_authored_methods:
        return []
    try:
        pom_src = pom_file.read_text(encoding="utf-8")
    except OSError:
        return []
    bodies = _java_method_bodies(pom_src, class_name) if is_java else _js_method_bodies(pom_src, class_name)
    if not bodies:
        return []

    # Compute base line offset for each method body so violations point
    # at the real POM line, not an offset within the body slice.
    method_line_starts = _compute_method_line_starts(pom_src, class_name, language=lang)

    violations: list[HygieneViolation] = []
    for method_name, body in bodies.items():
        if method_name not in agent_authored_methods:
            continue
        # Strip strings/comments in the body slice so a docstring or
        # comment containing `expect(` doesn't false-positive. Same
        # trick as `_scan_pom_assertions` in test_indexer.
        stripped = _js_strip(body)
        for pat in _POM_ASSERTION_PATTERNS:
            m = pat.search(stripped)
            if not m:
                continue
            # Line within the body (0-based) + method's base line.
            body_line_offset = stripped.count("\n", 0, m.start())
            base_line = method_line_starts.get(method_name, 0)
            line = base_line + body_line_offset
            snippet = _snippet(stripped, m.start()).strip()
            violations.append(HygieneViolation(
                rule="pom-assertion",
                file=str(pom_file),
                line=line or 1,
                method=method_name,
                message=(
                    f"contains {snippet!r} inside POM body. Assertions "
                    f"belong in test methods only — rewrite as a "
                    f"getter/probe returning the raw value and move the "
                    f"assertion to the test."
                ),
            ))
            break  # one violation per method is enough to fail the gate
    return violations


def _compute_method_line_starts(
    src: str, class_name: str, *, language: str = "typescript",
) -> dict[str, int]:
    """Return ``{method_name: 1-based line where the method body opens}``.

    Used to translate an offset inside a method body back to a real
    source line for violation messages. Best-effort; a missing entry
    just means the caller falls back to line 1.
    """
    stripped = _js_strip(src)
    body = _js_class_body(stripped, class_name)
    if body is None:
        return {}
    # Find offset of class body in the stripped source so we can add it
    # to method offsets inside `body` to get absolute stripped offsets.
    class_body_offset = stripped.find(body)
    if class_body_offset == -1:
        return {}
    is_java = (language or "").lower() in ("java", "selenium-java", "playwright-java")
    head_re = _JAVA_METHOD_HEAD_RE if is_java else _JS_METHOD_HEAD_RE
    lifecycle = _JAVA_LIFECYCLE_NAMES if is_java else _LIFECYCLE_NAMES
    out: dict[str, int] = {}
    seen: set[str] = set()
    for m in head_re.finditer(body):
        name = m.group(1)
        if name in lifecycle or name in seen:
            continue
        open_paren = m.end() - 1
        close = _find_balanced(body, open_paren)
        if close == -1:
            continue
        if is_java:
            # No return-type-after-colon ambiguity — just skip an optional
            # `throws X, Y` clause to the body `{` (or bail on a bare `;`
            # abstract/interface declaration).
            j = close + 1
            while j < len(body) and body[j] not in "{;":
                j += 1
            if j >= len(body) or body[j] != "{":
                continue
        else:
            tail = body[close + 1: close + 32].lstrip()
            if not tail or (tail[0] not in "{:" and not tail.startswith("=>")):
                continue
            # Advance to method-body opening `{` (angle-depth-aware — same
            # skip-past-return-type-generic dance as _js_method_bodies).
            j = _find_method_body_open(body, close)
            if j == -1:
                continue
        absolute_offset = class_body_offset + j
        # 1-based line number of the opening `{`.
        line = stripped.count("\n", 0, absolute_offset) + 1
        out[name] = line
        seen.add(name)
    return out


def _snippet(text: str, offset: int, *, length: int = 80) -> str:
    end = min(len(text), offset + length)
    return text[offset:end].split("\n", 1)[0]


# ---------------------------------------------------------------------------
# Fix 3 — return-consumption gate (Phase B.5.5)
# ---------------------------------------------------------------------------


def find_return_consumption_violations(
    pom_file: Path,
    class_name: str,
    agent_authored_methods: set[str],
    test_files: list[Path],
    *,
    language: str,
) -> list[HygieneViolation]:
    """For each agent-authored POM method whose EMITTED signature returns
    non-void, require the return value at every call site to be consumed.

    "Consumed" means assigned, destructured, wrapped in ``expect(...)``,
    ``return``ed, ``throw``n, or used as a sub-expression. A bare
    ``await pom.foo();`` statement is a violation.
    """
    if (language or "").lower() not in ("typescript", "javascript"):
        return []
    if not agent_authored_methods:
        return []
    try:
        pom_src = pom_file.read_text(encoding="utf-8")
    except OSError:
        return []

    # 1) Identify which authored methods actually return non-void as
    #    EMITTED (not as planned — the extender may have drifted from
    #    Promise<void> to Promise<{...}>, which is the whole point of
    #    this check).
    non_void = _non_void_agent_methods(pom_src, class_name, agent_authored_methods)
    if not non_void:
        return []

    violations: list[HygieneViolation] = []
    for tf in test_files:
        try:
            test_src = tf.read_text(encoding="utf-8")
        except OSError:
            continue
        aliases = _js_receiver_aliases(test_src, class_name)
        if not aliases:
            continue
        sanitized = _js_strip(test_src)
        lines = test_src.splitlines()
        for m in _JS_CALL_HEAD_RE.finditer(sanitized):
            obj_name, method_name = m.group(1), m.group(2)
            if obj_name not in aliases:
                continue
            if method_name not in non_void:
                continue
            if _is_call_consumed(sanitized, m.start()):
                continue
            line = sanitized.count("\n", 0, m.start()) + 1
            snippet = lines[line - 1].strip() if 0 < line <= len(lines) else ""
            violations.append(HygieneViolation(
                rule="return-consumption",
                file=str(tf),
                line=line,
                method=method_name,
                message=(
                    f"{class_name}.{method_name}() returns non-void but the "
                    f"call discards the result — wrap it in "
                    f"`expect(await ...).to*(EXPECTED)` or destructure the "
                    f"return, otherwise change the POM signature to "
                    f"`Promise<void>` and move the assertion into the test. "
                    f"[{snippet}]"
                ),
            ))
    return violations


# ---------------------------------------------------------------------------
# Return-type extraction and non-void classification
# ---------------------------------------------------------------------------


_VOID_RETURN_TYPES: frozenset[str] = frozenset({
    "void", "undefined", "promise<void>", "promise<undefined>",
    # `any` / `unknown` are too weak to bind on — treat as void so we
    # don't over-fail on legitimate probe-style methods whose return
    # type is deliberately opaque.
    "any", "unknown", "promise<any>", "promise<unknown>",
})


def _non_void_agent_methods(
    pom_src: str, class_name: str, agent_authored_methods: set[str],
) -> set[str]:
    """Return the subset of ``agent_authored_methods`` whose emitted
    signature declares a non-void return type."""
    stripped = _js_strip(pom_src)
    body = _js_class_body(stripped, class_name)
    if body is None:
        return set()
    out: set[str] = set()
    seen: set[str] = set()
    for m in _JS_METHOD_HEAD_RE.finditer(body):
        name = m.group(1)
        if name in _LIFECYCLE_NAMES or name in seen:
            continue
        if name not in agent_authored_methods:
            continue
        open_paren = m.end() - 1
        close = _find_balanced(body, open_paren)
        if close == -1:
            continue
        rt = _js_method_return_type(body, close)
        seen.add(name)
        if rt is None:
            # No annotation — TS infers, we can't easily know. Treat as
            # void (favour false-negative over false-positive on a gate
            # that shipping-blocks).
            continue
        if rt.strip().lower() in _VOID_RETURN_TYPES:
            continue
        out.add(name)
    return out


def _js_method_return_type(body: str, params_close_idx: int) -> str | None:
    """Given the index of the ``)`` closing a method's params inside
    ``body`` (already string/comment-stripped), return the return-type
    annotation as source text, or None when there is no annotation.

    Walks from ``)`` to the method-body opening ``{``, angle- and
    paren-depth-aware so ``Promise<{a: b}>`` is not mistaken for the
    method body opening. Handles two entry shapes for the return type:

      - Bare type after ``:`` (`Promise<T>`, ``void``, ``string | null``)
      - Object-literal type after ``:`` (``{a: b}``) — brace-matched
        before continuing the walk.
    """
    i = params_close_idx + 1
    # Skip whitespace between `)` and `:`.
    while i < len(body) and body[i] in " \t\r\n":
        i += 1
    if i >= len(body) or body[i] != ":":
        return None
    i += 1  # past `:`
    # Skip whitespace after `:`.
    while i < len(body) and body[i] in " \t\r\n":
        i += 1
    start = i
    # Handle object-literal return type: `: {a: b} {` — advance past
    # the matching `}` before continuing.
    if i < len(body) and body[i] == "{":
        close = _find_balanced_brace(body, i)
        if close == -1:
            return None
        i = close + 1
    # Walk forward to the method-body opening `{` at angle- and paren-
    # depth zero. `=>` (arrow) also terminates.
    angle_depth = 0
    paren_depth = 0
    while i < len(body):
        ch = body[i]
        if ch == "<":
            angle_depth += 1
        elif ch == ">":
            if angle_depth > 0:
                angle_depth -= 1
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
        elif ch == "{" and angle_depth == 0 and paren_depth == 0:
            return body[start:i].strip() or None
        elif (
            ch == "="
            and i + 1 < len(body)
            and body[i + 1] == ">"
            and angle_depth == 0
            and paren_depth == 0
        ):
            return body[start:i].strip() or None
        i += 1
    return None


# ---------------------------------------------------------------------------
# Test-side receiver resolution + consumption heuristic
# ---------------------------------------------------------------------------


def _js_receiver_aliases(test_src: str, class_name: str) -> set[str]:
    """Return the set of variable names in ``test_src`` that resolve to
    ``class_name`` — either via ``import { class_name }`` combined with
    ``const x = new class_name(...)``, or the class_name itself used as a
    receiver directly (static-method style — rare but harmless to include).
    """
    stripped = _js_strip(test_src)
    known = {class_name}
    # Confirm the class is imported in this file; if not, no receiver
    # binds to it here.
    imported = False
    for m in _JS_IMPORT_RE.finditer(stripped):
        for part in m.group(1).split(","):
            token = part.strip()
            if not token:
                continue
            orig = token.split(" as ", 1)[0].strip() if " as " in token else token
            if orig == class_name:
                imported = True
                break
        if imported:
            break
    if not imported:
        return set()
    aliases: set[str] = {class_name}
    for m in _JS_NEW_RE.finditer(stripped):
        inst, cls = m.group(1), m.group(2)
        if cls in known:
            aliases.add(inst)
    return aliases


# Characters immediately preceding the call that unambiguously mean
# "value is discarded" (i.e. this is a bare expression statement).
_DISCARD_PRECEDING: frozenset[str] = frozenset({";", "{", "}"})


def _is_call_consumed(sanitized: str, call_start: int) -> bool:
    """True when the call at ``call_start`` (inside stripped source) is
    used as an expression (assigned, wrapped, returned, etc.) rather
    than stated as a bare statement.

    Heuristic: walk backward from ``call_start`` skipping whitespace.
    If the first non-whitespace character is ``;``, ``{``, ``}``, or we
    hit the start of the buffer, the call is a bare expression statement
    → discarded. Anything else (``=``, ``(``, ``,``, ``return``,
    ``throw``, operators, ``.`` for chaining, etc.) means the call
    contributes to an enclosing expression → consumed.
    """
    i = call_start - 1
    while i >= 0 and sanitized[i] in " \t\r\n":
        i -= 1
    if i < 0:
        return False
    return sanitized[i] not in _DISCARD_PRECEDING
