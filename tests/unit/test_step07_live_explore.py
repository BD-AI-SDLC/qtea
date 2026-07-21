"""Step 7 pre-codegen live-exploration tests (live-map rendering)."""

from __future__ import annotations

import json

from qtea import schemas as qtea_schemas
from qtea.steps.s07_live_explore import (
    _DOM_PROBE_JS,
    LoginSpec,
    _build_explore_prompt,
    _build_explorer_budget_hook,
    _element_label,
    _extract_routes,
    _parse_live_map,
    _parse_targets,
    _render_existing_pages,
    _tool_suffix,
    build_observed_dev_pool,
    iter_observed_elements,
    render_live_map_for_codegen,
    render_live_map_for_prompt,
)


def _hook_cb(hooks):
    """Extract the single PreToolUse callback from a built hooks map."""
    return hooks["PreToolUse"][0].hooks[0]


def _decision(out):
    """PreToolUse permission decision (``None`` when the call is allowed)."""
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


async def _call(cb, tool_name):
    return await cb({"tool_name": tool_name}, "tid", {"signal": None})


def test_tool_suffix_parses_mcp_and_plain_names():
    assert _tool_suffix("mcp__playwright__browser_snapshot") == "browser_snapshot"
    assert _tool_suffix("Write") == "Write"
    assert _tool_suffix("") == ""


async def test_budget_hook_denies_consecutive_reads_then_progress_resets():
    """N inspection calls in a row are allowed; the (N+1)th is DENIED; a
    progress action (click) resets the streak so reads flow again."""
    hooks, state = _build_explorer_budget_hook(
        max_consecutive_reads=4,
    )
    cb = _hook_cb(hooks)
    snap = "mcp__playwright__browser_snapshot"
    # First 4 reads allowed.
    for _ in range(4):
        assert _decision(await _call(cb, snap)) is None
    # 5th consecutive read denied.
    assert _decision(await _call(cb, snap)) == "deny"
    # A denied call must NOT advance counters — still denied on retry.
    assert _decision(await _call(cb, snap)) == "deny"
    assert state["total_snapshots"] == 4  # denied snapshots not counted
    # A progress action clears the streak.
    assert _decision(await _call(cb, "mcp__playwright__browser_click")) is None
    # Reads flow again after real progress.
    assert _decision(await _call(cb, snap)) is None


async def test_budget_hook_write_is_progress_and_resets_streak():
    """`Write` (the incremental progress-map save) counts as progress, so it
    both is never denied and resets the read streak."""
    hooks, _ = _build_explorer_budget_hook(
        max_consecutive_reads=2,
    )
    cb = _hook_cb(hooks)
    ev = "mcp__playwright__browser_evaluate"
    assert _decision(await _call(cb, ev)) is None
    assert _decision(await _call(cb, ev)) is None
    assert _decision(await _call(cb, ev)) == "deny"
    assert _decision(await _call(cb, "Write")) is None  # save always allowed
    assert _decision(await _call(cb, ev)) is None  # streak reset by Write


async def test_budget_hook_total_snapshots_is_telemetry_only():
    """Snapshot counts accumulate for telemetry (surfaced in live_explore_done
    log) but never trigger a denial — the dollar ceiling is the only spend
    bound; a runaway snapshot loop is caught by the consecutive-read cap. Many
    snapshots interleaved with progress actions are allowed without cap."""
    hooks, state = _build_explorer_budget_hook(
        max_consecutive_reads=10,
    )
    cb = _hook_cb(hooks)
    snap = "mcp__playwright__browser_snapshot"
    click = "mcp__playwright__browser_click"
    # 50 snapshots interleaved with clicks — no denial, counter accumulates.
    for _ in range(50):
        assert _decision(await _call(cb, snap)) is None
        assert _decision(await _call(cb, click)) is None
    assert state["total_snapshots"] == 50
    assert state["denied"] == 0


async def test_budget_hook_ignores_non_browser_tools():
    """Non-browser, non-progress tools (Read/Glob) get no opinion and do not
    consume the read budget."""
    hooks, _ = _build_explorer_budget_hook(
        max_consecutive_reads=1,
    )
    cb = _hook_cb(hooks)
    # Many Reads never trip the cap (they're not inspection-of-page reads).
    for _ in range(5):
        assert _decision(await _call(cb, "Read")) is None
    # The single allowed browser read still works afterward.
    assert _decision(await _call(cb, "mcp__playwright__browser_snapshot")) is None


def test_render_distinguishes_auth_required_from_missing():
    """A login-gated route (auth_required) must be rendered as EXISTING and
    NOT as missing, so the architect plans it normally; only a genuine 404
    route is flagged as absent."""
    live_map = {
        "base_url": "https://qa.example.com",
        "routes": [
            {"path": "/", "exists": True, "auth_required": False,
             "notable_roles": ["button: Sign in"]},
            {"path": "/dashboard", "exists": False, "auth_required": True,
             "redirected_to": "https://qa.example.com/login"},
            {"path": "/nope", "exists": False, "auth_required": False},
        ],
    }
    out = render_live_map_for_prompt(live_map)

    # Auth-gated route: present, explicitly "not missing".
    assert "/dashboard" in out
    assert "behind login/SSO" in out
    assert "do NOT treat as missing" in out
    # Only the genuine 404 is flagged absent.
    assert "`/nope` — DOES NOT EXIST" in out
    assert "`/dashboard` — DOES NOT EXIST" not in out
    # Rendered app page keeps its observed roles.
    assert "button: Sign in" in out


def test_render_empty_when_no_routes():
    assert render_live_map_for_prompt(None) == ""
    assert render_live_map_for_prompt({"routes": []}) == ""
    assert render_live_map_for_prompt({"nope": 1}) == ""


def test_parse_live_map_accepts_auth_required_field():
    raw = (
        '{"base_url": "https://qa.example.com", "routes": ['
        '{"path": "/x", "exists": false, "auth_required": true, '
        '"redirected_to": "https://qa.example.com/login", "notable_roles": []}]}'
    )
    parsed = _parse_live_map(raw)
    assert parsed is not None
    assert parsed["routes"][0]["auth_required"] is True


def test_parse_live_map_rejects_non_route_shapes():
    assert _parse_live_map("not json") is None
    assert _parse_live_map('{"routes": "x"}') is None


def test_extract_routes_stays_on_origin_and_includes_root():
    strategy = (
        "Navigate to https://qa.example.com/entity/create and then to "
        "https://evil.example.net/steal — also /directory/list is referenced."
    )
    routes = _extract_routes(strategy, "https://qa.example.com", max_routes=12)
    assert "/" in routes
    assert "/entity/create" in routes
    assert "/directory/list" in routes
    # Off-origin URL is dropped (never navigate off-origin while authenticated).
    assert not any("evil" in r for r in routes)


# --- Comprehensive element capture + crawl ---------------------------------

_CRAWL_MAP = {
    "base_url": "https://qa.example.com/",
    "routes": [
        {
            "path": "/dashboard", "exists": True, "auth_required": False,
            "discovered_from": "/",
            "elements": [
                {"role": "button", "name": "New report", "test_id": "new-rpt"},
                {"role": "link", "name": "Settings", "test_id": None},
            ],
        },
        {"path": "/login", "exists": False, "auth_required": True, "elements": []},
        {"path": "/gone", "exists": False, "auth_required": False, "elements": []},
    ],
}


def test_render_lists_all_elements_no_eight_cap():
    """Structured elements are rendered comprehensively (the old ~8 cap is gone
    for the data itself; only a generous per-page display cap applies)."""
    out = render_live_map_for_prompt(_CRAWL_MAP)
    assert "button: New report [testid=new-rpt]" in out
    assert "link: Settings" in out


def test_iter_observed_elements_skips_gated_and_missing():
    rows = list(iter_observed_elements(_CRAWL_MAP))
    paths = {r[0] for r in rows}
    assert paths == {"/dashboard"}  # /login (gated) + /gone (missing) excluded
    roles_names = {(r[2], r[3]) for r in rows}
    assert ("button", "New report") in roles_names
    assert ("link", "Settings") in roles_names


def test_iter_observed_elements_tolerates_legacy_notable_roles():
    legacy = {"routes": [
        {"path": "/", "exists": True, "notable_roles": ["button: Save", "textbox: Q"]},
    ]}
    rows = list(iter_observed_elements(legacy))
    assert ("button", "Save", None) in [(r[2], r[3], r[4]) for r in rows]


def test_build_observed_dev_pool_prefers_testid_then_role_name():
    pool = build_observed_dev_pool(_CRAWL_MAP)["locators"]
    entries = list(pool.values())
    # test-id element -> data-testid selector. Payload now carries a `kind` so the
    # JIT runtime dispatches the typed getter (get_by_test_id) at action time.
    testid_entry = next(e for e in entries if e["selector"].startswith("[data-testid="))
    assert testid_entry["payload"] == {"kind": "test_id", "value": "new-rpt"}
    assert "New report" in testid_entry["intent"]
    # role+name element -> role= selector (never xpath)
    role_entry = next(e for e in entries if e["selector"].startswith("role="))
    assert role_entry["payload"] == {"kind": "role", "role": "link", "name": "Settings"}
    # page_url synthesised from base + path
    assert testid_entry["page_url"] == "https://qa.example.com/dashboard"


def test_build_observed_dev_pool_empty_when_nothing_observed():
    assert build_observed_dev_pool(None) == {"locators": {}}
    assert build_observed_dev_pool({"routes": []}) == {"locators": {}}


def test_render_for_codegen_only_existing_pages():
    out = render_live_map_for_codegen(_CRAWL_MAP)
    assert "/dashboard" in out
    assert "/login" not in out  # gated page contributes no codegen grounding
    assert 'tbd("intent")' in out  # instructs tbd fallback for unlisted elements


def test_explore_prompt_authenticated_and_bounds():
    p_auth = _build_explore_prompt(
        "https://qa.example.com", ["/", "/x"],
        test_context="", authenticated=True,
        max_pages=10, max_reveals_per_page=4,
    )
    assert "PRE-AUTHENTICATED" in p_auth
    assert "at most 10 pages" in p_auth
    assert "4 reveal actions per page" in p_auth
    assert "SAME ORIGIN" in p_auth
    # Targeted visit: no link discovery, only the given target routes.
    assert "NOT a crawl" in p_auth
    assert "/x" in p_auth  # target route is listed

    p_unauth = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="", authenticated=False,
        max_pages=5, max_reveals_per_page=2,
    )
    assert "NOT authenticated" in p_unauth
    assert "2 reveal actions per page" in p_unauth


def test_explore_prompt_is_test_driven_and_excludes_content_links():
    """The prompt must inline the test context, target only tested pages, and
    exclude content/data links (cost + relevance)."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="TC-1: user logs in and creates an entity.",
        authenticated=True, max_pages=12, max_reveals_per_page=4,
    )
    assert "WHAT IS UNDER TEST" in p
    assert "creates an entity" in p          # test context is inlined
    assert "targeted visit" in p
    assert "PRIMARY NAVIGATION" in p
    for excluded in ("data-table", "pagination", "external"):
        assert excluded in p


def test_explore_prompt_allows_reveal_but_forbids_mutation():
    """Reveal actions (open dialog/form) are permitted; mutations are not."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="create a report", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "REVEAL" in p
    assert "NON-DESTRUCTIVE" in p
    # Explicitly forbids committing/mutating clicks.
    for forbidden in ("submit", "save", "delete"):
        assert forbidden in p


def test_explore_prompt_without_test_context_falls_back():
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "no test design was supplied" in p


def test_explore_prompt_inlines_existing_pages_as_existence_context():
    """Step 6's known page objects are inlined as existence context, and the
    prompt must state they never widen scope (targeted-visit invariant)."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="open Records", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
        existing_pages="  - RecordsPage (src/pages/records.py)",
    )
    assert "KNOWN EXISTING PAGES" in p
    assert "RecordsPage" in p
    assert "NEVER to add pages the tests don't touch" in p


def test_explore_prompt_inlines_nav_vocabulary():
    """The live-harvested primary-nav labels are inlined so the explorer maps
    tested features to real pages; the block must state it never widens scope."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="open Records", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
        nav_vocabulary="  - Entity Directory\n  - Dashboard",
    )
    assert "APP NAVIGATION VOCABULARY" in p
    assert "Entity Directory" in p
    assert "does NOT widen scope" in p
    # Omitted entirely when nothing was harvested.
    p2 = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="open Records", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "APP NAVIGATION VOCABULARY" not in p2


async def test_harvest_nav_labels_empty_when_playwright_unavailable(monkeypatch):
    """The harvest is best-effort: with qtea's Playwright missing it returns []
    (the explorer then discovers the nav live) rather than raising."""
    from qtea import headed_auth_capture as hac

    monkeypatch.setattr(hac, "is_available", lambda: False)
    out = await hac.harvest_nav_labels("https://qa.example.com", None)
    assert out == []


def test_render_existing_pages_from_sut_inventory():
    """Page-object class names (or names) + files are rendered from Step 6's
    sut_inventory; missing/malformed inventories yield an empty string."""
    research = {
        "sut_inventory": {
            "modules": [
                {"existing_page_objects": [
                    {"class_name": "LoginPage", "file": "src/pages/login.py"},
                    {"name": "records", "file": "src/pages/records.py"},
                    {"class_name": "LoginPage", "file": "dup.py"},  # dedup by name
                ]},
            ],
        },
    }
    out = _render_existing_pages(research)
    assert "LoginPage (src/pages/login.py)" in out
    assert "records (src/pages/records.py)" in out
    assert out.count("LoginPage") == 1  # deduplicated

    assert _render_existing_pages(None) == ""
    assert _render_existing_pages({}) == ""
    assert _render_existing_pages({"sut_inventory": {"modules": []}}) == ""


# --- MCP login (Plan A) -----------------------------------------------------


def test_explore_prompt_includes_login_block_when_login_given():
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="log in and create a report", authenticated=False,
        max_pages=12, max_reveals_per_page=4,
        login=LoginSpec(username="alice", password="pw-12345", provider="Internal"),
    )
    assert "STEP 0 — LOG IN" in p
    assert "alice" in p and "pw-12345" in p         # credentials embedded to type
    assert "Internal" in p                          # provider hint honored
    assert "AUTHENTICATE YOURSELF" in p
    # After login it must revert to observe+reveal (the login is the exception).
    assert "STEP 0 login" in p


def test_explore_prompt_provider_default_avoids_sso():
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="", authenticated=False,
        max_pages=12, max_reveals_per_page=4,
        login=LoginSpec(username="alice", password="pw-12345", provider=None),
    )
    assert "avoid SSO" in p or "avoid SSO / single-sign-on" in p


def test_explore_prompt_no_login_block_without_login():
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="", authenticated=False,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "STEP 0 — LOG IN" not in p


# --- DOM-verified test_id capture -------------------------------------------


def test_explore_prompt_embeds_dom_probe_and_forbids_guessing():
    """The prompt must embed the DOM-verification probe verbatim and forbid
    fabricating a locator from the accessible name/context. The probe now emits
    a full verified `locator` object (Step-7 #4), so the forbid-guessing wording
    covers "locator", not just the legacy "test_id"."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert _DOM_PROBE_JS in p
    assert "browser_evaluate" in p
    assert "NEVER invent, guess, or paraphrase a locator" in p
    assert "verified_unique" in p
    assert "locator_ambiguous" in p
    assert "ambiguity_reason" in p


def test_explore_prompt_documents_bounded_ref_scoped_fallback():
    """A correlation tie (2+ probe entries matching one AOM element) must be
    resolvable via a bounded, ref-scoped browser_evaluate re-probe."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "ref" in p
    assert "5 such targeted calls per page" in p


def test_element_label_tolerates_locator_ambiguous_elements():
    """Elements carrying the new optional locator_ambiguous/ambiguity_reason
    keys must render without error, with test_id still treated as absent."""
    el = {
        "role": "link", "name": "Management systems", "test_id": None,
        "locator_ambiguous": True,
        "ambiguity_reason": "data-test shared by 4 sibling cards",
    }
    # No verified locator + no test_id, but the probe flagged an honest
    # ambiguity -> the label surfaces "[ambiguous]" rather than a fabricated
    # locator (Step-7 #4).
    label = _element_label(el)
    assert label == "link: Management systems [ambiguous]"

    live_map = {
        "base_url": "https://qa.example.com",
        "routes": [
            {"path": "/dashboard", "exists": True, "auth_required": False,
             "discovered_from": "/", "elements": [el]},
        ],
    }
    # Downstream consumers must not choke on the new keys.
    # iter_observed_elements now yields a 6-tuple ending in the verified
    # locator object (None here, since the element is ambiguous).
    rows = list(iter_observed_elements(live_map))
    assert rows == [
        ("/dashboard", None, "link", "Management systems", None, None)
    ]
    assert "Management systems" in render_live_map_for_codegen(live_map)
    assert "Management systems" in render_live_map_for_prompt(live_map)
    # No verified locator + no test_id -> the dev-pool falls through to the
    # legacy role+name tier, now tagged with a `kind` so the runtime picks the
    # typed Playwright getter.
    pool = build_observed_dev_pool(live_map)["locators"]
    assert any(
        e["payload"] == {"kind": "role", "role": "link",
                         "name": "Management systems"}
        for e in pool.values()
    )


def test_live_map_schema_accepts_verified_and_ambiguous_elements():
    live_map = {
        "base_url": "https://qa.example.com",
        "routes": [
            {
                "path": "/", "exists": True, "auth_required": False,
                "redirected_to": None, "discovered_from": None,
                "elements": [
                    {"role": "button", "name": "Sign in", "test_id": "signin-btn"},
                    {
                        "role": "link", "name": "Management systems",
                        "test_id": None, "locator_ambiguous": True,
                        "ambiguity_reason": "data-test shared by 4 sibling cards",
                    },
                ],
            },
        ],
    }
    ok, err = qtea_schemas.is_valid(live_map, "live-map")
    assert ok, err


def test_live_map_schema_rejects_missing_required_route_fields():
    bad = {"routes": [{"path": "/", "elements": []}]}  # missing exists/auth_required
    ok, err = qtea_schemas.is_valid(bad, "live-map")
    assert not ok


# --- MCP-availability safety net --------------------------------------------


async def test_live_explore_skips_write_when_playwright_mcp_pending(
    tmp_path, monkeypatch,
):
    """If the Playwright MCP was `pending`/`failed` at agent init, the explorer
    had no browser tools and its JSON is meaningless — the pass must return None
    and write NO live-map.json rather than persisting a misleading `{routes:[]}`
    that looks like "explored, found nothing"."""
    from pathlib import Path

    from qtea import storage_state as _storage_state
    from qtea.claude_runner import AgentResult
    from qtea.steps import s07_live_explore as mod

    monkeypatch.setenv("SUT_BASE_URL", "https://qa.example.com/app/")
    monkeypatch.delenv("QTEA_LIVE_EXPLORE", raising=False)
    monkeypatch.delenv("QTEA_NO_LLM_RESOLVE", raising=False)

    # Warm is best-effort; stub it so the test never spawns npx.
    monkeypatch.setattr(mod, "_warm_playwright_mcp", lambda _env: None)
    monkeypatch.setattr(_storage_state, "resolve", lambda **_kw: None)

    async def _fake_run_agent(*_a, **_kw):
        # Agent "succeeds" (end_turn) but never saw the playwright tools.
        return AgentResult(
            success=True, exit_code=0, duration_s=1.0,
            transcript_path=Path("t"), stderr_path=Path("s"),
            metrics_path=Path("m"),
            final_text='{"base_url": "https://qa.example.com/app/", "routes": []}',
            mcp_servers_pending=["playwright"],
        )

    monkeypatch.setattr(mod, "run_agent", _fake_run_agent)

    out_dir = tmp_path / "artifacts" / "step07"
    out_dir.mkdir(parents=True)
    result = await mod.explore_strategy_routes(
        strategy_text="TC-1: open /dashboard",
        research=None,
        sut_root=tmp_path / "sut",
        workspace_root=tmp_path,
        out_dir=out_dir,
        workdir=tmp_path / "wd",
    )

    assert result is None
    assert not (out_dir / "live-map.json").exists()


async def test_live_explore_keeps_map_when_pending_but_routes_present(
    tmp_path, monkeypatch,
):
    """`pending`-at-init is NOT proof of failure — a slow MCP can recover before
    the agent needs it and still complete a full visit. If the agent produced
    routes, the tools clearly worked, so the map must be KEPT even though
    playwright appears in mcp_servers_pending (avoids false-discarding good
    exploration)."""
    from pathlib import Path

    from qtea import storage_state as _storage_state
    from qtea.claude_runner import AgentResult
    from qtea.steps import s07_live_explore as mod

    monkeypatch.setenv("SUT_BASE_URL", "https://qa.example.com/app/")
    monkeypatch.delenv("QTEA_LIVE_EXPLORE", raising=False)
    monkeypatch.delenv("QTEA_NO_LLM_RESOLVE", raising=False)
    monkeypatch.setattr(mod, "_warm_playwright_mcp", lambda _env: None)
    monkeypatch.setattr(_storage_state, "resolve", lambda **_kw: None)

    async def _fake_run_agent(*_a, **_kw):
        return AgentResult(
            success=True, exit_code=0, duration_s=1.0,
            transcript_path=Path("t"), stderr_path=Path("s"),
            metrics_path=Path("m"),
            final_text=(
                '{"base_url": "https://qa.example.com/app/", "routes": ['
                '{"path": "/", "exists": true, "auth_required": false, '
                '"elements": [{"role": "button", "name": "Sign in", '
                '"test_id": "signin"}]}]}'
            ),
            mcp_servers_pending=["playwright"],  # pending at init, but recovered
        )

    monkeypatch.setattr(mod, "run_agent", _fake_run_agent)

    out_dir = tmp_path / "artifacts" / "step07"
    out_dir.mkdir(parents=True)
    result = await mod.explore_strategy_routes(
        strategy_text="TC-1: open /",
        research=None,
        sut_root=tmp_path / "sut",
        workspace_root=tmp_path,
        out_dir=out_dir,
        workdir=tmp_path / "wd",
    )

    assert result is not None
    assert (out_dir / "live-map.json").exists()
    assert result["routes"][0]["path"] == "/"


# --- Snapshot guardrail (giant-table blow-up) -------------------------------


def test_explore_prompt_has_snapshot_discipline_guardrail():
    """The prompt must guard against the giant-table snapshot blow-up: scope the
    FIRST snapshot (never shrink after), never loop re-snapshotting a huge node,
    never chunk-read a spilled tool-result file, and hard-cap snapshots per
    page."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="open the notifications table", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "SNAPSHOT DISCIPLINE" in p
    assert "SCOPE FIRST" in p  # scope the first snapshot, don't trim afterwards
    assert "at MOST 3 snapshots per page" in p
    assert "HARD CAP" in p  # the 3-snapshot cap is stated as non-negotiable
    assert "spilled" in p  # forbids chunk-reading spilled tool-result files
    assert "depth" in p and "ref" in p  # the scoping levers


def test_explore_prompt_has_incremental_save_block():
    """The prompt must instruct the explorer to persist its accumulating map to
    the progress file after each page, so a max-turns/timeout cutoff before the
    final JSON emit still leaves a recoverable partial map on disk."""
    from qtea.steps import s07_live_explore as mod

    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="visit the dashboard", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "INCREMENTAL SAVE" in p
    assert mod._PROGRESS_MAP_NAME in p  # the exact filename the fallback reads
    assert "Write" in p  # tells the agent which tool to use


def test_read_progress_map_recovers_partial(tmp_path):
    """A completed incremental write is recovered and parsed as a live-map."""
    from qtea.steps import s07_live_explore as mod

    partial = {
        "base_url": "https://qa.example.com",
        "routes": [
            {"path": "/", "exists": True, "auth_required": False, "elements": []},
        ],
    }
    (tmp_path / mod._PROGRESS_MAP_NAME).write_text(
        json.dumps(partial), encoding="utf-8",
    )
    recovered = mod._read_progress_map(tmp_path)
    assert recovered is not None
    assert [r["path"] for r in recovered["routes"]] == ["/"]


def test_read_progress_map_absent_returns_none(tmp_path):
    """No progress file → None (nothing to salvage)."""
    from qtea.steps import s07_live_explore as mod

    assert mod._read_progress_map(tmp_path) is None


def test_read_progress_map_unparseable_returns_none(tmp_path):
    """A garbage/truncated file yields None rather than raising."""
    from qtea.steps import s07_live_explore as mod

    (tmp_path / mod._PROGRESS_MAP_NAME).write_text("{not json", encoding="utf-8")
    assert mod._read_progress_map(tmp_path) is None


async def test_live_explore_salvages_partial_map_on_max_turns(tmp_path, monkeypatch):
    """The key robustness guarantee: when the explorer is cut off before emitting
    its final JSON (max-turns → success=False, final_text is prose), the map it
    wrote incrementally to disk is recovered so the pre-pass yields a partial map
    instead of failing. Mirrors run 20260709: the agent died mid-visit but had
    captured the root page."""
    from pathlib import Path

    from qtea import storage_state as _storage_state
    from qtea.claude_runner import AgentResult
    from qtea.steps import s07_live_explore as mod

    monkeypatch.setenv("SUT_BASE_URL", "https://qa.example.com/app/")
    monkeypatch.delenv("QTEA_LIVE_EXPLORE", raising=False)
    monkeypatch.delenv("QTEA_NO_LLM_RESOLVE", raising=False)
    monkeypatch.setattr(mod, "_warm_playwright_mcp", lambda _env: None)
    monkeypatch.setattr(_storage_state, "resolve", lambda **_kw: None)

    async def _no_targets(*_a, **_kw):
        return {"targets": [], "routes": []}

    monkeypatch.setattr(mod, "_extract_semantic_targets", _no_targets)

    async def _fake_run_agent(*_a, **_kw):
        # The explorer captured the root page and persisted it incrementally,
        # then ran out of turns before emitting the final JSON.
        wd = _kw["workdir"]
        wd.mkdir(parents=True, exist_ok=True)
        (wd / mod._PROGRESS_MAP_NAME).write_text(
            '{"base_url": "https://qa.example.com/app/", "routes": ['
            '{"path": "/", "exists": true, "auth_required": false, '
            '"elements": [{"role": "button", "name": "Sign in", '
            '"test_id": "signin"}]}]}',
            encoding="utf-8",
        )
        return AgentResult(
            success=False, exit_code=-1, duration_s=1.0,
            transcript_path=Path("t"), stderr_path=Path("s"),
            metrics_path=Path("m"),
            final_text="Good, this loaded the Basics tab.",  # prose, not JSON
            error="Reached maximum number of turns (48)",
        )

    monkeypatch.setattr(mod, "run_agent", _fake_run_agent)

    out_dir = tmp_path / "artifacts" / "step07"
    out_dir.mkdir(parents=True)
    result = await mod.explore_strategy_routes(
        strategy_text="TC-1: open /",
        research=None,
        sut_root=tmp_path / "sut",
        workspace_root=tmp_path,
        out_dir=out_dir,
        workdir=tmp_path / "wd",
    )

    assert result is not None  # NOT None despite the max-turns failure
    assert (out_dir / "live-map.json").exists()  # canonical map still written
    assert result["routes"][0]["path"] == "/"
    assert result["routes"][0]["elements"][0]["name"] == "Sign in"


async def test_live_explore_no_output_when_no_final_and_no_progress(
    tmp_path, monkeypatch,
):
    """When the agent fails AND wrote no progress file, there is nothing to
    salvage → the pre-pass returns None (unchanged degradation).

    The deterministic Playwright fallback (Step-7 #7) is disabled here
    (`QTEA_LIVE_EXPLORE_FALLBACK=0`) so this test isolates the pure
    agent-failed-no-salvage degradation path; the fallback has its own tests."""
    from pathlib import Path

    from qtea import storage_state as _storage_state
    from qtea.claude_runner import AgentResult
    from qtea.steps import s07_live_explore as mod

    monkeypatch.setenv("SUT_BASE_URL", "https://qa.example.com/app/")
    monkeypatch.setenv("QTEA_LIVE_EXPLORE_FALLBACK", "0")
    monkeypatch.delenv("QTEA_LIVE_EXPLORE", raising=False)
    monkeypatch.delenv("QTEA_NO_LLM_RESOLVE", raising=False)
    monkeypatch.setattr(mod, "_warm_playwright_mcp", lambda _env: None)
    monkeypatch.setattr(_storage_state, "resolve", lambda **_kw: None)

    async def _no_targets(*_a, **_kw):
        return {"targets": [], "routes": []}

    monkeypatch.setattr(mod, "_extract_semantic_targets", _no_targets)

    async def _fake_run_agent(*_a, **_kw):
        return AgentResult(
            success=False, exit_code=-1, duration_s=1.0,
            transcript_path=Path("t"), stderr_path=Path("s"),
            metrics_path=Path("m"),
            final_text="", error="boom",
        )

    monkeypatch.setattr(mod, "run_agent", _fake_run_agent)

    out_dir = tmp_path / "artifacts" / "step07"
    out_dir.mkdir(parents=True)
    result = await mod.explore_strategy_routes(
        strategy_text="TC-1: open /",
        research=None,
        sut_root=tmp_path / "sut",
        workspace_root=tmp_path,
        out_dir=out_dir,
        workdir=tmp_path / "wd",
    )

    assert result is None
    assert not (out_dir / "live-map.json").exists()


# --- Semantic target checklist (Fix 3) --------------------------------------


def test_explore_prompt_renders_named_targets_block():
    """Semantic targets from the extractor render as an explicit checklist that
    states it does NOT widen scope; omitted entirely when none supplied."""
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="notifications", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
        named_targets=[
            {"name": "My Notifications inbox",
             "reach_via": "My Pages menu -> My Notifications",
             "why": "asserts subject line"},
            {"name": "Owner view"},
        ],
    )
    assert "TESTED TARGETS" in p
    assert "My Notifications inbox" in p
    assert "My Pages menu -> My Notifications" in p
    assert "does NOT widen scope" in p

    p2 = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="notifications", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
    )
    assert "TESTED TARGETS" not in p2


def test_named_targets_block_skips_entries_without_name():
    p = _build_explore_prompt(
        "https://qa.example.com", ["/"],
        test_context="x", authenticated=True,
        max_pages=12, max_reveals_per_page=4,
        named_targets=[{"reach_via": "somewhere"}, "garbage", {"name": "Real"}],
    )
    assert "TESTED TARGETS" in p
    assert "Real" in p
    assert "somewhere" not in p  # entry with no name dropped


# --- Semantic target extraction parsing -------------------------------------


def test_parse_targets_tolerates_fences_and_normalizes():
    raw = (
        "```json\n"
        '{"targets": [{"name": "My Notifications", "reach_via": "nav"}], '
        '"routes": ["/notifications", 5]}\n'
        "```"
    )
    out = _parse_targets(raw)
    assert out is not None
    assert out["targets"] == [{"name": "My Notifications", "reach_via": "nav"}]
    assert out["routes"] == ["/notifications"]  # non-string dropped


def test_parse_targets_recovers_from_surrounding_prose():
    raw = 'Here you go: {"targets": [], "routes": []} — done.'
    assert _parse_targets(raw) == {"targets": [], "routes": []}


def test_parse_targets_missing_keys_yields_empty_lists():
    assert _parse_targets("{}") == {"targets": [], "routes": []}
    # Malformed target entries are dropped, not fatal.
    assert _parse_targets('{"targets": ["x", 1], "routes": "nope"}') == {
        "targets": [], "routes": [],
    }


def test_parse_targets_returns_none_on_non_object():
    assert _parse_targets("not json at all") is None
    assert _parse_targets("[1, 2, 3]") is None


def test_live_explore_targets_schema_accepts_extractor_output():
    ok, err = qtea_schemas.is_valid(
        {"targets": [{"name": "Inbox", "reach_via": "nav", "why": "asserts"}],
         "routes": ["/inbox"]},
        "live-explore-targets",
    )
    assert ok, err
    # name is required on a target.
    ok2, _ = qtea_schemas.is_valid(
        {"targets": [{"reach_via": "nav"}], "routes": []},
        "live-explore-targets",
    )
    assert not ok2


# --- Turn-budget derivation (Fix 1) -----------------------------------------


async def _run_explore_capturing_kwargs(tmp_path, monkeypatch, *, targets, routes_final):
    """Helper: run explore_strategy_routes with all LLM/browser calls stubbed and
    return the kwargs run_agent was invoked with (so we can assert max_turns)."""
    from pathlib import Path

    from qtea import storage_state as _storage_state
    from qtea.claude_runner import AgentResult
    from qtea.steps import s07_live_explore as mod

    monkeypatch.setenv("SUT_BASE_URL", "https://qa.example.com/app/")
    monkeypatch.delenv("QTEA_LIVE_EXPLORE", raising=False)
    monkeypatch.delenv("QTEA_NO_LLM_RESOLVE", raising=False)
    monkeypatch.setattr(mod, "_warm_playwright_mcp", lambda _env: None)
    monkeypatch.setattr(_storage_state, "resolve", lambda **_kw: None)

    async def _fake_extract(*_a, **_kw):
        return {"targets": targets, "routes": []}

    monkeypatch.setattr(mod, "_extract_semantic_targets", _fake_extract)

    captured: dict = {}

    async def _fake_run_agent(*_a, **kw):
        captured.update(kw)
        route_json = ",".join(
            f'{{"path": "{r}", "exists": true, "auth_required": false, '
            f'"elements": []}}'
            for r in routes_final
        )
        return AgentResult(
            success=True, exit_code=0, duration_s=1.0,
            transcript_path=Path("t"), stderr_path=Path("s"),
            metrics_path=Path("m"),
            final_text='{"base_url": "https://qa.example.com/app/", "routes": ['
            + route_json + "]}",
        )

    monkeypatch.setattr(mod, "run_agent", _fake_run_agent)

    out_dir = tmp_path / "artifacts" / "step07"
    out_dir.mkdir(parents=True)
    await mod.explore_strategy_routes(
        strategy_text="Notification feature in-app inbox; no URLs in this prose.",
        research=None,
        sut_root=tmp_path / "sut",
        workspace_root=tmp_path,
        out_dir=out_dir,
        workdir=tmp_path / "wd",
    )
    return captured, mod


async def test_turn_budget_floored_for_single_route_spa(tmp_path, monkeypatch):
    """A prose test design yields only `/` lexically; the budget must still meet
    the floor so a launcher SPA (feature behind in-app nav) is not starved."""
    monkeypatch.delenv("QTEA_LIVE_EXPLORE_MAX_TURNS", raising=False)
    captured, mod = await _run_explore_capturing_kwargs(
        tmp_path, monkeypatch,
        targets=[{"name": "My Notifications", "reach_via": "My Pages -> Inbox"}],
        routes_final=["/"],
    )
    assert captured["max_turns"] >= mod._MIN_TURNS


async def test_turn_budget_scales_above_floor_with_many_targets(tmp_path, monkeypatch):
    """With enough semantic targets the reveal-aware derivation must exceed the
    floor (targets drive the budget, not just the lexical route count). Uses
    enough targets to clear the raised `_MIN_TURNS` floor (Step-7 #1)."""
    monkeypatch.delenv("QTEA_LIVE_EXPLORE_MAX_TURNS", raising=False)
    monkeypatch.delenv("QTEA_LIVE_EXPLORE_MAX_REVEALS_PER_PAGE", raising=False)
    from qtea.steps import s07_live_explore as _mod
    # Pick a target count whose derived budget clears the floor with margin.
    # per_page = 4 + 2*4 = 12; derived = (n+1)*12 + 20 must exceed _MIN_TURNS.
    per_page = _mod._TURN_FIXED_PER_PAGE + _mod._TURN_PER_REVEAL * 4
    n = ((_mod._MIN_TURNS - _mod._TURN_HEADROOM) // per_page) + 5
    many = [{"name": f"Target {i}"} for i in range(n)]
    captured, mod = await _run_explore_capturing_kwargs(
        tmp_path, monkeypatch, targets=many, routes_final=["/"],
    )
    # target_units = max(1, n+1); per_page = 4 + 2*4 = 12.
    expected = (n + 1) * (mod._TURN_FIXED_PER_PAGE + mod._TURN_PER_REVEAL * 4) + mod._TURN_HEADROOM
    assert captured["max_turns"] == expected
    assert captured["max_turns"] > mod._MIN_TURNS


async def test_turn_budget_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("QTEA_LIVE_EXPLORE_MAX_TURNS", "7")
    captured, _mod = await _run_explore_capturing_kwargs(
        tmp_path, monkeypatch,
        targets=[{"name": "X"}], routes_final=["/"],
    )
    assert captured["max_turns"] == 7


# --- extract_semantic_targets best-effort behavior --------------------------


async def test_extract_semantic_targets_best_effort_on_error(tmp_path, monkeypatch):
    from qtea.steps import s07_live_explore as mod

    async def _boom(*_a, **_kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(mod, "call_reasoning_llm", _boom)
    out = await mod._extract_semantic_targets(
        "some test design", workdir=tmp_path / "te",
    )
    assert out == {"targets": [], "routes": []}


async def test_extract_semantic_targets_parses_success(tmp_path, monkeypatch):
    from pathlib import Path

    from qtea.claude_runner import AgentResult
    from qtea.steps import s07_live_explore as mod

    async def _ok(*_a, **_kw):
        return AgentResult(
            success=True, exit_code=0, duration_s=1.0,
            transcript_path=Path("t"), stderr_path=Path("s"),
            metrics_path=Path("m"),
            final_text='```json\n{"targets": [{"name": "Inbox"}], '
            '"routes": ["/inbox"]}\n```',
        )

    monkeypatch.setattr(mod, "call_reasoning_llm", _ok)
    out = await mod._extract_semantic_targets(
        "test design text", workdir=tmp_path / "te",
    )
    assert out["targets"] == [{"name": "Inbox"}]
    assert out["routes"] == ["/inbox"]


async def test_extract_semantic_targets_empty_input_short_circuits(tmp_path, monkeypatch):
    from qtea.steps import s07_live_explore as mod

    called = {"n": 0}

    async def _count(*_a, **_kw):
        called["n"] += 1

    monkeypatch.setattr(mod, "call_reasoning_llm", _count)
    out = await mod._extract_semantic_targets("   ", workdir=tmp_path / "te")
    assert out == {"targets": [], "routes": []}
    assert called["n"] == 0  # no LLM call on empty design


# --- Cost ceiling ($20 primary throttle, Step-7 #1) -------------------------


async def test_cost_ceiling_denies_browser_tools_but_not_write():
    """Once the running estimated cost reaches the ceiling, EVERY non-Write tool
    call is denied (so the agent stops spending), but Write stays allowed so it
    can persist its progress map and emit the final JSON."""
    hooks, state = _build_explorer_budget_hook(
        max_consecutive_reads=1000, max_cost_usd=20.0,
    )
    cb = _hook_cb(hooks)
    snap = "mcp__playwright__browser_snapshot"
    # Below the ceiling: browser tools flow.
    assert _decision(await _call(cb, snap)) is None
    # Simulate spend crossing the ceiling (the on_event callback would do this
    # live in a real run).
    state["cost_usd"] = 20.0
    assert _decision(await _call(cb, snap)) == "deny"
    assert _decision(await _call(cb, "mcp__playwright__browser_click")) == "deny"
    assert state["cost_ceiling_hit"] is True
    # Write is the exception — the agent must still be able to finalize.
    assert _decision(await _call(cb, "Write")) is None


async def test_no_cost_ceiling_when_model_cost_unknown():
    """When cost can't be estimated (max_cost_usd=None), the ceiling is inert —
    only the consecutive-read anti-rat-hole cap remains."""
    hooks, state = _build_explorer_budget_hook(
        max_consecutive_reads=1000, max_cost_usd=None,
    )
    cb = _hook_cb(hooks)
    state["cost_usd"] = 9999.0  # would blow any real ceiling
    assert _decision(await _call(cb, "mcp__playwright__browser_snapshot")) is None
    assert state["cost_ceiling_hit"] is False


def test_cost_tracker_accumulates_usage_and_snaps_to_sdk_total():
    """The on_event tracker adds per-message usage-derived cost live, and snaps
    UP to the SDK's authoritative total_cost_usd when present (never down)."""
    from qtea.steps import s07_live_explore as mod

    state = {"cost_usd": 0.0}
    # A known model so estimate_cost returns a non-zero figure.
    tracker = mod._make_cost_tracker(state, "claude-sonnet-4-5")
    assert tracker is not None
    tracker({"usage": {"input_tokens": 1_000_000, "output_tokens": 0}})
    after_usage = state["cost_usd"]
    assert after_usage > 0.0
    # SDK final figure higher -> snap up.
    tracker({"total_cost_usd": after_usage + 5.0})
    assert state["cost_usd"] == after_usage + 5.0
    # A lower SDK figure must NOT lower the running estimate.
    tracker({"total_cost_usd": 0.01})
    assert state["cost_usd"] == after_usage + 5.0


def test_cost_tracker_none_when_model_unknown():
    """Unknown model -> no tracker (caller then disables the dollar ceiling)."""
    from qtea.steps import s07_live_explore as mod

    assert mod._make_cost_tracker({"cost_usd": 0.0}, None) is None


# --- reach_via reconciliation (Step-7 #2/#3) --------------------------------


def test_reconcile_reach_via_maps_to_real_nav_label():
    from qtea.steps import s07_live_explore as mod

    labels = ["Entity Directory", "My Notifications", "Settings"]
    # Substring containment -> the real, fuller label.
    assert (
        mod._reconcile_reach_via("Directory", labels)
        == "Entity Directory"
    )
    # Exact (case-insensitive).
    assert mod._reconcile_reach_via("settings", labels) == "Settings"
    # Fuzzy over the first hop.
    assert mod._reconcile_reach_via("My Pages -> notifications", labels) == "My Notifications"


def test_reconcile_reach_via_returns_none_when_no_confident_match():
    from qtea.steps import s07_live_explore as mod

    labels = ["Dashboard", "Reports", "Admin"]
    assert mod._reconcile_reach_via("Completely Unrelated Widget", labels) is None
    assert mod._reconcile_reach_via("anything", []) is None
    assert mod._reconcile_reach_via("", labels) is None


def test_reconcile_targets_attaches_nav_label_without_mutating_input():
    from qtea.steps import s07_live_explore as mod

    src = [
        {"name": "Inbox", "reach_via": "notifications"},
        {"name": "Orphan", "reach_via": "no such menu at all"},
    ]
    labels = ["My Notifications", "Billing"]
    out = mod._reconcile_targets(src, labels)
    assert out[0]["nav_label"] == "My Notifications"
    assert out[0]["reach_via"] == "notifications"  # original preserved
    assert "nav_label" not in out[1]  # unmatched left as-is
    assert "nav_label" not in src[0]  # input never mutated


# --- verified-locator dev-pool (Step-7 #4) ----------------------------------


def test_dev_pool_prefers_verified_locator_object():
    """A DOM-verified locator object drives the dev-pool selector/payload,
    resolving a role-strategy locator to the element's real role (not its name)."""
    live_map = {
        "base_url": "https://qa.example.com",
        "routes": [
            {
                "path": "/rpt", "exists": True, "auth_required": False,
                "discovered_from": None,
                "elements": [
                    {
                        "role": "button", "name": "Save",
                        "locator": {
                            "strategy": "role", "value": "Save", "name": "Save",
                            "verified_unique": True,
                        },
                        "test_id": "rpt-save",
                    },
                ],
            },
        ],
    }
    pool = build_observed_dev_pool(live_map)["locators"]
    entries = list(pool.values())
    assert len(entries) == 1
    # Role strategy resolves to the element's real role in the selector.
    assert entries[0]["selector"] == 'role=button[name="Save"]'
    assert entries[0]["payload"]["kind"] == "role"
    assert entries[0]["payload"]["role"] == "button"
    assert entries[0]["payload"]["name"] == "Save"


def test_dev_pool_ignores_unverified_locator():
    """A locator object missing verified_unique must NOT be trusted; the pool
    falls back to test_id (then role+name)."""
    live_map = {
        "base_url": "https://qa.example.com",
        "routes": [
            {
                "path": "/x", "exists": True, "auth_required": False,
                "discovered_from": None,
                "elements": [
                    {
                        "role": "button", "name": "Go",
                        "locator": {"strategy": "role", "value": "Go"},
                        "test_id": "go-btn",
                    },
                ],
            },
        ],
    }
    pool = build_observed_dev_pool(live_map)["locators"]
    entries = list(pool.values())
    assert entries[0]["payload"] == {"kind": "test_id", "value": "go-btn"}


def test_dev_pool_label_strategy_emits_cross_language_css():
    """A verified `label` locator must map to a `[aria-label="..."]` CSS selector,
    NOT `text=` — the JS/Java runtimes feed `selector` straight to page.locator,
    where `text=` matches the visible label text (wrong node) and no public
    `label=` engine exists. Selector and payload must be identical CSS."""
    live_map = {
        "base_url": "https://qa.example.com",
        "routes": [
            {
                "path": "/form", "exists": True, "auth_required": False,
                "discovered_from": None,
                "elements": [
                    {
                        "role": "textbox", "name": "Email address",
                        "locator": {
                            "strategy": "label", "value": "Email address",
                            "verified_unique": True,
                        },
                        "test_id": None,
                    },
                ],
            },
        ],
    }
    pool = build_observed_dev_pool(live_map)["locators"]
    entries = list(pool.values())
    assert len(entries) == 1
    assert entries[0]["selector"] == '[aria-label="Email address"]'
    assert entries[0]["payload"] == {
        "kind": "css", "selector": '[aria-label="Email address"]',
    }
    assert "text=" not in entries[0]["selector"]


# --- entry_element (reach-path capture, roleless launcher tiles) ------------


def _map_with_entry_element():
    """Live-map fixture: home has one tile the explorer clicked to reach
    /reports. The tile is the SUT's launcher pattern — roleless <div> located
    by visible text — captured via the probe's roleless fallback."""
    return {
        "base_url": "https://qa.example.com",
        "routes": [
            {
                "path": "/", "url": "https://qa.example.com/",
                "exists": True, "auth_required": False,
                "discovered_from": None,
                "elements": [
                    {
                        "role": "button", "name": "User menu",
                        "locator": {
                            "strategy": "id", "value": "userMenu",
                            "verified_unique": True,
                        },
                        "test_id": "userMenu",
                    },
                ],
            },
            {
                "path": "/reports", "url": "https://qa.example.com/reports",
                "exists": True, "auth_required": False,
                "discovered_from": "/",
                "entry_element": {
                    "role": "generic", "name": "Reports Solution",
                    "locator": {
                        "strategy": "text", "value": "Reports Solution",
                        "verified_unique": True,
                    },
                    "test_id": None,
                },
                "elements": [
                    {
                        "role": "button", "name": "New report",
                        "locator": {
                            "strategy": "test_id", "value": "new-rpt",
                            "verified_unique": True,
                        },
                        "test_id": "new-rpt",
                    },
                ],
            },
        ],
    }


def test_live_map_schema_accepts_entry_element():
    """The schema must accept a non-root route with an `entry_element` of the
    same shape as an elements[] entry (roleless launcher tile via text)."""
    ok, err = qtea_schemas.is_valid(_map_with_entry_element(), "live-map")
    assert ok, f"schema rejected entry_element: {err}"


def test_iter_observed_elements_yields_entry_element_under_parent():
    """entry_element must be yielded attributed to the PARENT route's path/url
    (where the click happens), NOT the child route (where the click lands)."""
    tuples = list(iter_observed_elements(_map_with_entry_element()))
    entry_tuples = [t for t in tuples if t[3] == "Reports Solution"]
    assert len(entry_tuples) == 1
    path, url, role, name, tid, loc = entry_tuples[0]
    assert path == "/"                                # parent path, not "/reports"
    assert url == "https://qa.example.com/"           # parent url
    assert role == "generic"
    assert loc == {"strategy": "text", "value": "Reports Solution",
                   "verified_unique": True}


def test_dev_pool_includes_entry_element_locator():
    """The dev-pool must expose the entry_element as a first-class locator so
    codegen's beforeEach can lift it — with a valid Playwright `text=` string
    selector matching the roleless-tile capture."""
    pool = build_observed_dev_pool(_map_with_entry_element())["locators"]
    entries = [e for e in pool.values() if e["intent"].startswith("Reports Solution")]
    assert len(entries) == 1
    assert entries[0]["selector"] == "text=Reports Solution"
    assert entries[0]["payload"] == {"kind": "text", "text": "Reports Solution"}
    assert entries[0].get("page_url") == "https://qa.example.com/"


def test_render_live_map_for_prompt_surfaces_entry_hop():
    """The architect-facing render must show `← from <parent> via <label>` so
    the plan can wire the parent-side click into beforeEach."""
    out = render_live_map_for_prompt(_map_with_entry_element())
    assert "← from `/` via" in out
    assert "Reports Solution" in out


def test_render_live_map_for_codegen_prefixes_entry_element():
    """Codegen-facing render lists the reach hop as a first line prefixed
    `← entry from <parent>`, keeping it visually distinct from page elements."""
    out = render_live_map_for_codegen(_map_with_entry_element())
    assert "← entry from `/`" in out
    assert "Reports Solution" in out


# --- deterministic probe -> elements (Step-7 #7) ----------------------------


def test_probe_output_to_elements_marks_only_genuine_ambiguity():
    from qtea.steps import s07_live_explore as mod

    probe = [
        {"role": "button", "name": "Save",
         "locator": {"strategy": "id", "value": "save", "verified_unique": True},
         "testId": "save"},
        {"role": "link", "name": "Home", "locator": None, "testId": None},  # unique
        {"role": "link", "name": "Dup", "locator": None, "testId": None},   # ambiguous
        {"role": "link", "name": "Dup", "locator": None, "testId": None},
    ]
    els = mod._probe_output_to_elements(probe)
    by_name = {e["name"]: e for e in els if e["name"] != "Dup"}
    # Verified locator carried through verbatim.
    assert by_name["Save"]["locator"]["verified_unique"] is True
    # Unique role+name, no locator -> NOT flagged ambiguous.
    assert "locator_ambiguous" not in by_name["Home"]
    # Duplicated role+name with no locator -> flagged ambiguous with a reason.
    dups = [e for e in els if e["name"] == "Dup"]
    assert dups and all(e.get("locator_ambiguous") is True for e in dups)
    assert all(e.get("ambiguity_reason") for e in dups)


def test_probe_output_to_elements_tolerates_json_string_and_junk():
    from qtea.steps import s07_live_explore as mod

    assert mod._probe_output_to_elements("not json") == []
    assert mod._probe_output_to_elements(None) == []
    js = json.dumps([{"role": "button", "name": "OK",
                      "locator": {"strategy": "id", "value": "ok",
                                  "verified_unique": True}}])
    els = mod._probe_output_to_elements(js)
    assert els[0]["name"] == "OK"
    assert els[0]["locator"]["value"] == "ok"
