// @ts-check
/**
 * qtea JIT locator runtime — vendored into TypeScript / JavaScript +
 * Playwright SUTs at Step 7 codegen time.
 *
 * Single-file CommonJS module. Patches `Page.prototype.locator`,
 * `Frame.prototype.locator`, and `Locator.prototype.locator` on
 * `playwright-core` to intercept sentinel strings produced by `tbd()`.
 * Resolution chain matches the Python runtime:
 *
 *   1. dev-locators file (zero LLM)
 *   2. runtime cache (zero LLM)
 *   3. in-process AOM heuristic (zero LLM)
 *   4. LLM via ResolverServer over loopback TCP (one LLM call per cold miss)
 *   5. test fails fast with a structured diagnostic
 *
 * Wraps returned `Locator` instances in an ES6 Proxy that retries the
 * failing action once on `TimeoutError` after cache-invalidate + re-resolve.
 *
 * Setup hooks (registered by Step 7's `_vendor_typescript_playwright_runtime`):
 *
 *   - Playwright Test:  `playwright.config.{ts,js}` → `globalSetup: "./tests/qtea-runtime"`
 *   - Jest:             `jest.config.{js,ts}` → `setupFiles: ["<rootDir>/tests/qtea-runtime.js"]`
 *   - Vitest:           `vitest.config.{ts,js}` → `test.setupFiles: ["./tests/qtea-runtime.js"]`
 *
 * ENV VARS (mirror the Python runtime — set by Step 8):
 *
 *   - QTEA_CACHE_DIR        directory holding locator-cache.json (required)
 *   - QTEA_RESOLVER_PORT    TCP port of parent ResolverServer (preferred)
 *   - QTEA_RESOLVER_TOKEN   per-run shared secret authenticating the bridge
 *   - QTEA_DEV_LOCATORS     optional dev-supplied locator file
 *   - QTEA_DEFAULT_TIMEOUT_MS  Playwright default timeout (default 60000)
 *   - QTEA_INFLATE_TIMEOUTS    "0" opts out of timeout inflation
 *   - QTEA_DISABLE_JIT         "1" disables the monkey-patch entirely
 *   - QTEA_NO_LLM_RESOLVE      "1" disables tier 4 (cache+dev+heuristic only)
 *   - QTEA_RUN_ID              stamped into cache entries
 */

"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");
const crypto = require("crypto");

// ---------------------------------------------------------------------------
// Sentinel
// ---------------------------------------------------------------------------

const SENTINEL_PREFIX = "__QTEA_TBD__::";

/**
 * Mark a locator constant as unresolved. The intent string describes what
 * the element is supposed to be, in plain English.
 * @param {string} intent
 * @returns {string}
 */
function tbd(intent) {
  if (typeof intent !== "string" || !intent.trim()) {
    throw new Error("tbd() requires a non-empty intent string");
  }
  return SENTINEL_PREFIX + intent.trim();
}

/** @param {unknown} value @returns {value is string} */
function isSentinel(value) {
  return typeof value === "string" && value.startsWith(SENTINEL_PREFIX);
}

/** @param {string} value @returns {string} */
function parseSentinel(value) {
  return value.slice(SENTINEL_PREFIX.length);
}

// ---------------------------------------------------------------------------
// Logger (minimal, structured)
// ---------------------------------------------------------------------------

function log(event, /** @type {Record<string, unknown>} */ fields) {
  const line = JSON.stringify({ event, ...(fields || {}) });
  try {
    process.stderr.write("qtea " + line + "\n");
  } catch (_) {
    // logging must never throw
  }
}

// ---------------------------------------------------------------------------
// Cache (JSON file at $QTEA_CACHE_DIR/locator-cache.json)
// ---------------------------------------------------------------------------

function cachePath() {
  const base = process.env.QTEA_CACHE_DIR;
  return base ? path.join(base, "locator-cache.json") : null;
}

function readCache() {
  const p = cachePath();
  if (!p || !fs.existsSync(p)) return {};
  try {
    const raw = JSON.parse(fs.readFileSync(p, "utf8"));
    if (!raw || !Array.isArray(raw.entries)) return {};
    /** @type {Record<string, any>} */
    const out = {};
    for (const e of raw.entries) {
      if (e && typeof e === "object" && e.key) out[e.key] = e;
    }
    return out;
  } catch (_) {
    return {};
  }
}

function writeCache(/** @type {Record<string, any>} */ entries) {
  const p = cachePath();
  if (!p) return;
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const payload = {
    run_id: process.env.QTEA_RUN_ID || null,
    produced_at: new Date().toISOString(),
    entries: Object.values(entries),
  };
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(payload, null, 2), "utf8");
  fs.renameSync(tmp, p);
}

/**
 * Stable cache key (mirrors Python `cache_key`).
 * @param {string | null} testFile
 * @param {string} constantName
 * @param {string} intent
 */
function cacheKey(testFile, constantName, intent) {
  const norm = (intent || "").trim().toLowerCase().replace(/\s+/g, " ");
  const payload = `${testFile || ""}::${constantName}::${norm}`;
  return crypto.createHash("sha256").update(payload).digest("hex").slice(0, 16);
}

function invalidateCacheEntry(/** @type {string} */ key) {
  const cache = readCache();
  if (cache[key]) {
    delete cache[key];
    try {
      writeCache(cache);
      log("cache_invalidated", { key });
    } catch (e) {
      log("cache_invalidate_failed", { error: String(e) });
    }
  }
}

/**
 * Rewrite the cache entry so the working fallback becomes the sole entry.
 * Called after a fallback candidate survives an action that timed out under
 * the original primary — the failed primary is dropped on the theory that
 * it timed out under the inflated 60s timeout and is therefore broken
 * rather than slow. Mirrors Python `_promote_candidate_in_cache`.
 * @param {string} key  cache key
 * @param {{selector: string, strategy?: string | null, confidence?: number | null}} working
 */
function promoteCandidateInCache(key, working) {
  const cache = readCache();
  const entry = cache[key];
  if (!entry) return;
  entry.selector = working.selector;
  entry.strategy = working.strategy ?? null;
  entry.confidence = working.confidence ?? null;
  entry.candidates = [working];
  cache[key] = entry;
  try {
    writeCache(cache);
    log("fallback_promoted", { key, selector: working.selector });
  } catch (e) {
    log("fallback_promote_failed", { error: String(e) });
  }
}

// ---------------------------------------------------------------------------
// Dev-locators (vendored mini-loader)
// ---------------------------------------------------------------------------

function isXpath(/** @type {string} */ s) {
  const t = (s || "").trim();
  return t.startsWith("//") || t.startsWith("xpath=") || t.includes("By.XPATH");
}

/** @type {Record<string, {selector: string, strategy: string | null, intent: string | null}> | null} */
let devLocatorsCache = null;

function loadDevLocators() {
  /** @type {string[]} */
  const candidates = [];
  if (process.env.QTEA_DEV_LOCATORS) candidates.push(process.env.QTEA_DEV_LOCATORS);
  candidates.push(path.join(process.cwd(), ".qtea", "dev-locators.json"));
  for (const p of candidates) {
    if (!fs.existsSync(p) || !fs.statSync(p).isFile()) continue;
    try {
      const raw = JSON.parse(fs.readFileSync(p, "utf8"));
      const block = raw && typeof raw === "object" ? raw.locators : null;
      if (!block || typeof block !== "object") continue;
      /** @type {Record<string, any>} */
      const out = {};
      for (const [name, entry] of Object.entries(block)) {
        if (!entry || typeof entry !== "object") continue;
        // @ts-expect-error narrowing
        const sel = entry.selector;
        if (typeof sel !== "string" || !sel.trim() || isXpath(sel)) continue;
        out[name] = {
          selector: sel.trim(),
          // @ts-expect-error narrowing
          strategy: typeof entry.strategy === "string" ? entry.strategy : null,
          // @ts-expect-error narrowing
          intent: typeof entry.intent === "string" ? entry.intent : null,
        };
      }
      log("dev_locators_loaded", { path: p, count: Object.keys(out).length });
      return out;
    } catch (_) {
      continue;
    }
  }
  return {};
}

// ---------------------------------------------------------------------------
// Tier-3 heuristic resolver — port of the Python implementation
// ---------------------------------------------------------------------------

const ROLE_KEYWORDS = {
  button: "button", submit: "button", btn: "button",
  link: "link", anchor: "link",
  tab: "tab",
  input: "textbox", field: "textbox", textbox: "textbox", textfield: "textbox",
  checkbox: "checkbox",
  radio: "radio",
  dropdown: "combobox", select: "combobox", combobox: "combobox",
  menu: "menu", menuitem: "menuitem",
  heading: "heading", title: "heading", header: "heading",
  image: "img", icon: "img", img: "img",
  form: "form",
  dialog: "dialog", modal: "dialog",
  alert: "alert", banner: "banner",
  list: "list", listitem: "listitem",
  row: "row", cell: "cell", columnheader: "columnheader",
  tooltip: "tooltip",
  tree: "tree", treeitem: "treeitem",
  switch: "switch", toggle: "switch",
  slider: "slider",
  spinbutton: "spinbutton",
  search: "search", searchbox: "searchbox",
  navigation: "navigation", nav: "navigation",
};

const NAME_FILLERS = new Set([
  "the", "a", "an", "on", "in", "of", "for", "to", "with", "by",
  "primary", "main", "secondary",
]);

const HEURISTIC_MIN_SCORE = 0.9;
const HEURISTIC_TIE_GAP = 0.1;

/**
 * @param {string} intent
 * @returns {{role: string | null, nameTokens: string[], nameHint: string}}
 */
function parseIntent(intent) {
  const tokens = (intent || "").toLowerCase().split(/\W+/).filter(Boolean);
  /** @type {string | null} */
  let role = null;
  /** @type {string[]} */
  const nameTokens = [];
  for (const t of tokens) {
    if (role === null && t in ROLE_KEYWORDS) {
      // @ts-expect-error - index access
      role = ROLE_KEYWORDS[t];
      continue;
    }
    if (NAME_FILLERS.has(t)) continue;
    if (t in ROLE_KEYWORDS) continue;
    nameTokens.push(t);
  }
  return { role, nameTokens, nameHint: nameTokens.join(" ") };
}

/**
 * @param {any} node
 * @param {(n: any) => void} visit
 * @param {number} depth
 */
function aomWalk(node, visit, depth) {
  if (!node || typeof node !== "object" || depth > 50) return;
  visit(node);
  if (Array.isArray(node.children)) {
    for (const c of node.children) aomWalk(c, visit, depth + 1);
  }
}

/** @param {string} role @param {string} name */
function formatRoleSelector(role, name) {
  const escaped = name.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  return `role=${role}[name="${escaped}"]`;
}

/**
 * @param {string} intent
 * @param {any} snapshot
 * @returns {string | null}
 */
function heuristicResolve(intent, snapshot) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const { role, nameTokens, nameHint } = parseIntent(intent);
  if (!role || nameTokens.length === 0) return null;

  /** @type {Array<[number, string]>} */
  const candidates = [];
  aomWalk(snapshot, (node) => {
    if (node.role !== role) return;
    const nodeName = String(node.name || "").toLowerCase();
    if (!nodeName) return;
    if (nameHint && nodeName.includes(nameHint)) {
      candidates.push([1.0, node.name]);
    } else if (nameTokens.length && nameTokens.every((t) => nodeName.includes(t))) {
      candidates.push([0.95, node.name]);
    } else if (nameTokens.length && nameTokens.some((t) => nodeName.includes(t))) {
      candidates.push([0.6, node.name]);
    }
  }, 0);

  if (!candidates.length) return null;
  candidates.sort((a, b) => b[0] - a[0]);
  const [topScore, topName] = candidates[0];
  if (topScore < HEURISTIC_MIN_SCORE) return null;
  if (candidates.length > 1 && topScore - candidates[1][0] < HEURISTIC_TIE_GAP) return null;
  return formatRoleSelector(role, topName);
}

// ---------------------------------------------------------------------------
// Resolver client (TCP bridge to the parent ResolverServer)
// ---------------------------------------------------------------------------

const SOCKET_TIMEOUT_MS = 180000;
const SOCKET_MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

/**
 * Sync TCP roundtrip. Uses a tiny event-loop blocking trick (deasync-free)
 * via `Atomics.wait` on a shared buffer — but that requires worker setup.
 * Instead we expose an async resolver and wrap the entire monkey-patched
 * call sites in async-aware paths (Playwright's `locator()` is sync but
 * the returned locator's methods are async — perfect for us, since we
 * only need to resolve when an action runs).
 *
 * Implementation note: Playwright's Page.locator() is SYNCHRONOUS — it
 * builds a locator object without touching the browser. We therefore
 * defer resolution to the first ACTION call. That's also what the Python
 * runtime does for retries; here we just do it for the initial call too.
 *
 * @param {{intent: string, constantName: string, snapshotText: string,
 *          testFile: string | null, pageUrl: string | null}} req
 * @returns {Promise<{selector: string | null, strategy: string | null,
 *           confidence: number | null, source: string,
 *           snapshotHash: string | null, reason: string | null} | null>}
 */
function callResolverSocket(req) {
  const port = parseInt(process.env.QTEA_RESOLVER_PORT || "", 10);
  const token = process.env.QTEA_RESOLVER_TOKEN || "";
  if (!port || !token) return Promise.resolve(null);

  return new Promise((resolve) => {
    const sock = new net.Socket();
    /** @type {Buffer[]} */
    const chunks = [];
    let received = 0;
    let done = false;

    const finish = (/** @type {any} */ payload) => {
      if (done) return;
      done = true;
      try { sock.destroy(); } catch (_) {}
      resolve(payload);
    };

    sock.setTimeout(SOCKET_TIMEOUT_MS, () => {
      log("resolver_socket_timeout", {});
      finish(null);
    });
    sock.on("error", (e) => {
      log("resolver_socket_error", { error: String(e) });
      finish(null);
    });
    sock.on("data", (chunk) => {
      chunks.push(chunk);
      received += chunk.length;
      if (received > SOCKET_MAX_RESPONSE_BYTES) {
        log("resolver_socket_response_too_large", { bytes: received });
        finish(null);
        return;
      }
      // Look for newline terminator
      const buf = Buffer.concat(chunks);
      const nl = buf.indexOf(0x0a);
      if (nl !== -1) {
        try {
          const payload = JSON.parse(buf.slice(0, nl).toString("utf8"));
          if (!payload.ok) {
            log("resolver_socket_server_error", { error: payload.error });
            finish(null);
          } else {
            // candidates: ranked bundle (primary + optional fallback) for
            // the runtime's TimeoutError retry path. Absent in legacy
            // server responses — the proxy degrades gracefully when None.
            finish({
              selector: payload.selector ?? null,
              strategy: payload.strategy ?? null,
              confidence: payload.confidence ?? null,
              source: payload.source ?? "agent",
              snapshotHash: payload.snapshot_hash ?? null,
              reason: payload.reason ?? null,
              candidates: Array.isArray(payload.candidates) && payload.candidates.length
                ? payload.candidates
                : null,
            });
          }
        } catch (e) {
          log("resolver_socket_bad_json", { error: String(e) });
          finish(null);
        }
      }
    });
    sock.on("end", () => finish(null));

    sock.connect(port, "127.0.0.1", () => {
      const wire = JSON.stringify({
        token,
        intent: req.intent,
        constant_name: req.constantName,
        snapshot_text: req.snapshotText,
        test_file: req.testFile,
        page_url: req.pageUrl,
        source_type: "aom",
      }) + "\n";
      sock.write(wire);
    });
  });
}

// ---------------------------------------------------------------------------
// Resolution orchestrator
// ---------------------------------------------------------------------------

/** @typedef {{
 *    selector: string | null,
 *    source: "dev" | "cached" | "heuristic" | "agent" | "none",
 *    constantName: string,
 *    intent: string,
 *    testFile: string | null,
 *    candidates?: Array<{selector: string, strategy?: string | null, confidence?: number | null, reason?: string | null}> | null
 * }} Resolution
 */

// ---------------------------------------------------------------------------
// AOM snapshot — Locator.ariaSnapshot capability ladder + iframe enumeration.
// Mirrors the Python runtime. Rungs: A (mode+boxes, 1.60+) → B (mode, 1.59)
// → C (no-opts, 1.49-1.58; iframes enumerated manually) → D (legacy <1.49).
// Return split: `text` has iframes (for LLM); `dict` is main-frame-only
// (for tier-3 heuristic — avoids wrong-scope selectors).
// ---------------------------------------------------------------------------

/** @type {{modeAi: boolean | null, boxes: boolean | null}} */
const AOM_CAPS = { modeAi: null, boxes: null };

function readAomEnv() {
  let depth = null;
  const rawDepth = process.env.QTEA_AOM_DEPTH;
  if (rawDepth) {
    const parsed = parseInt(rawDepth, 10);
    if (Number.isInteger(parsed) && parsed > 0) depth = parsed;
  }
  const mode = String(process.env.QTEA_AOM_BOXES || "auto").trim().toLowerCase();
  const wantBoxes = mode !== "off";
  const forceBoxes = mode === "force";
  const legacyOk = process.env.QTEA_AOM_LEGACY_OK !== "0";
  return { depth, wantBoxes, forceBoxes, legacyOk };
}

/**
 * Build the kwarg ladder honouring the capability cache. Rungs proven
 * unsupported are skipped.
 * @param {{depth: number | null, wantBoxes: boolean, forceBoxes: boolean}} env
 * @returns {Array<Record<string, any>>}
 */
function aomKwargLadder(env) {
  /** @type {Array<Record<string, any>>} */
  const rungs = [];
  // Rung A: mode='ai' + boxes=true (+depth) — Playwright 1.60+
  if (
    env.wantBoxes
    && AOM_CAPS.modeAi !== false
    && (env.forceBoxes || AOM_CAPS.boxes !== false)
  ) {
    /** @type {Record<string, any>} */
    const kw = { mode: "ai", boxes: true };
    if (env.depth !== null) kw.depth = env.depth;
    rungs.push(kw);
  }
  // Rung B: mode='ai' (+depth) — Playwright 1.59
  if (AOM_CAPS.modeAi !== false) {
    /** @type {Record<string, any>} */
    const kw = { mode: "ai" };
    if (env.depth !== null) kw.depth = env.depth;
    rungs.push(kw);
  }
  // Rung C: no opts — Playwright 1.49-1.58 (iframes must be enumerated)
  rungs.push({});
  return rungs;
}

/** Cache an option-shape rejection. Attribute to most-recently-added opt.
 * @param {Record<string, any>} opts */
function updateAomCapsFromFailure(opts) {
  if (opts.boxes === true) { AOM_CAPS.boxes = false; return; }
  if (opts.mode === "ai") { AOM_CAPS.modeAi = false; }
}
/** @param {Record<string, any>} opts */
function updateAomCapsFromSuccess(opts) {
  if (opts.mode === "ai") AOM_CAPS.modeAi = true;
  if (opts.boxes === true) AOM_CAPS.boxes = true;
}

/**
 * Detect a Playwright JS "unknown option" error. Node throws different
 * error shapes across Playwright versions; catch anything that looks like
 * a signature mismatch and let the caller move down the ladder.
 * @param {any} err
 */
function isSignatureError(err) {
  if (!err) return false;
  if (err.name === "TypeError") return true;
  const msg = String(err.message || err);
  return /unknown|unexpected|invalid/i.test(msg);
}

/**
 * Call Locator.ariaSnapshot with the richest supported opts. Descends the
 * kwarg ladder on signature failures; propagates non-signature errors so
 * the caller can decide whether to fall through to legacy.
 * @param {any} bodyLocator
 * @param {{depth: number | null, wantBoxes: boolean, forceBoxes: boolean}} env
 * @returns {Promise<string>}
 */
async function callAriaSnapshot(bodyLocator, env) {
  let lastErr = null;
  for (const opts of aomKwargLadder(env)) {
    try {
      const keys = Object.keys(opts);
      // Playwright JS ariaSnapshot takes a single options object (or no arg).
      const result = keys.length === 0
        ? await bodyLocator.ariaSnapshot()
        : await bodyLocator.ariaSnapshot(opts);
      updateAomCapsFromSuccess(opts);
      return result || "";
    } catch (e) {
      if (isSignatureError(e)) {
        updateAomCapsFromFailure(opts);
        lastErr = e;
        continue;
      }
      throw e;
    }
  }
  if (lastErr) throw lastErr;
  return "";
}

/**
 * Best-effort iframe label: url() → name() → "unknown". Frame.url and
 * Frame.name are methods on Playwright JS Frame.
 * @param {any} frame
 * @returns {string}
 */
function iframeLabel(frame) {
  try {
    if (typeof frame.url === "function") {
      const u = frame.url();
      if (u) return String(u);
    }
  } catch (_) {}
  try {
    if (typeof frame.name === "function") {
      const n = frame.name();
      if (n) return String(n);
    }
  } catch (_) {}
  return "unknown";
}

/**
 * Enumerate iframes and append each non-main frame's snapshot to `mainText`.
 * Called on Rung C where Playwright does NOT include iframe subtrees.
 * Marker: `# iframe: <label>` — `#` prefix is ignored by parseAriaSnapshotYaml.
 * @param {any} page
 * @param {string} mainText
 * @param {{depth: number | null, wantBoxes: boolean, forceBoxes: boolean}} env
 * @returns {Promise<string>}
 */
async function appendIframeSnapshots(page, mainText, env) {
  let frames;
  try {
    frames = typeof page.frames === "function" ? page.frames() : page.frames;
  } catch (_) {
    return mainText;
  }
  if (!Array.isArray(frames)) return mainText;
  let mainFrame = null;
  try {
    mainFrame = typeof page.mainFrame === "function" ? page.mainFrame() : page.mainFrame;
  } catch (_) {}
  const parts = [mainText];
  for (const frame of frames.filter((f) => f !== mainFrame)) {
    let sub = "";
    try {
      const body = frame.locator("body");
      if (!body || typeof body.ariaSnapshot !== "function") continue;
      sub = await callAriaSnapshot(body, env);
    } catch (e) {
      log("iframe_snapshot_skip", { error: String(e) });
      continue;
    }
    if (!sub) continue;
    parts.push(`# iframe: ${iframeLabel(frame)}`);
    parts.push(sub);
  }
  return parts.join("\n");
}

// ---------------------------------------------------------------------------
// YAML parser for Locator.ariaSnapshot() output — port of the Python
// `_parse_aria_snapshot_yaml`. Produces the same {role, name, children}
// shape the heuristicResolve expects. Skips lines that don't start with
// `- ` (comment/marker lines, blank lines, attribute metadata under `/`).
// ---------------------------------------------------------------------------

const AOM_RE_BOX = /\s*\[box=([\d.,\-]+)\]/;
const AOM_RE_REF = /\s*\[ref=e?\d+\]/;
const AOM_RE_ATTR = /\s*\[[A-Za-z_][A-Za-z0-9_-]*=[^\]]*\]/;
const AOM_RE_QUOTED = /^([A-Za-z][A-Za-z0-9_-]*)\s+"((?:[^"\\]|\\.)*)"(.*)$/;
const AOM_RE_INLINE = /^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$/;
const AOM_RE_ROLE_ONLY = /^([A-Za-z][A-Za-z0-9_-]*).*$/;

/**
 * Parse Locator.ariaSnapshot() YAML into a dict tree compatible with
 * aomWalk / heuristicResolve. Returns `{}` for empty input.
 * @param {string} yamlText
 * @returns {any}
 */
function parseAriaSnapshotYaml(yamlText) {
  if (!yamlText || !yamlText.trim()) return {};

  /** @type {Array<any>} */
  const rootChildren = [];
  // Stack: [indent, childListToAppendInto]
  /** @type {Array<[number, Array<any>]>} */
  const stack = [[-1, rootChildren]];

  for (const rawLine of yamlText.split("\n")) {
    if (!rawLine.trim()) continue;
    const indent = rawLine.length - rawLine.trimStart().length;
    const line = rawLine.trim();
    if (!line.startsWith("- ")) continue;  // skips `# iframe:` markers etc.
    let body = line.slice(2).trim();
    if (body.startsWith("/")) continue;  // attribute metadata, not a node

    // Extract + strip annotations before role/name parsing.
    /** @type {[number, number, number, number] | null} */
    let box = null;
    const mBox = AOM_RE_BOX.exec(body);
    if (mBox) {
      const parts = mBox[1].split(",");
      if (parts.length === 4) {
        const nums = parts.map((p) => parseFloat(p));
        if (nums.every((n) => Number.isFinite(n))) {
          box = [nums[0], nums[1], nums[2], nums[3]];
        }
      }
      body = body.replace(AOM_RE_BOX, "");
    }
    body = body.replace(AOM_RE_REF, "");
    body = body.replace(AOM_RE_ATTR, "").trim();

    if (body.endsWith(":")) body = body.slice(0, -1).trimEnd();

    let role = "";
    let name = "";
    const mQ = AOM_RE_QUOTED.exec(body);
    if (mQ) {
      role = mQ[1];
      name = mQ[2];
    } else {
      const mI = AOM_RE_INLINE.exec(body);
      if (mI) {
        role = mI[1];
        const inlineText = mI[2].trim();
        if (inlineText) name = inlineText;
      } else {
        const mR = AOM_RE_ROLE_ONLY.exec(body);
        if (!mR) continue;
        role = mR[1];
      }
    }

    /** @type {any} */
    const node = { role, name, children: [] };
    if (box !== null) node.box = box;

    // Pop stack until parent indent is strictly less than current.
    while (stack.length > 1 && stack[stack.length - 1][0] >= indent) {
      stack.pop();
    }
    stack[stack.length - 1][1].push(node);
    stack.push([indent, node.children]);
  }

  return { role: "document", name: "", children: rootChildren };
}

/** Capture AOM via the modern Locator.ariaSnapshot API with capability
 * ladder + iframe enumeration + legacy accessibility fallback.
 * @param {any} page
 * @returns {Promise<{text: string, dict: any}>}
 */
async function snapshotPage(page) {
  const env = readAomEnv();
  // ---- Primary: Locator.ariaSnapshot (Playwright JS 1.49+) ----
  try {
    if (page && typeof page.locator === "function") {
      const body = page.locator("body");
      if (body && typeof body.ariaSnapshot === "function") {
        const mainText = await callAriaSnapshot(body, env);
        // Parse the MAIN-FRAME dict BEFORE appending iframe text so the
        // tier-3 heuristic never sees iframe-scoped elements.
        const dict = parseAriaSnapshotYaml(mainText);
        let fullText;
        if (AOM_CAPS.modeAi !== true) {
          fullText = await appendIframeSnapshots(page, mainText, env);
        } else {
          fullText = mainText;
        }
        return { text: fullText, dict };
      }
    }
  } catch (e) {
    log("snapshot_failed_aria", { error: String(e) });
    // Fall through — older Playwright JS might still expose the legacy API.
  }

  // ---- Legacy: page.accessibility.snapshot() (Playwright JS <1.49; also
  // works on 1.49-1.56 where it was deprecated but not yet removed) ----
  if (!env.legacyOk) return { text: "", dict: {} };
  try {
    if (page && page.accessibility && typeof page.accessibility.snapshot === "function") {
      const ax = (await page.accessibility.snapshot()) || {};
      return { text: JSON.stringify(ax), dict: ax };
    }
  } catch (e) {
    log("snapshot_failed_legacy", { error: String(e) });
  }
  return { text: "", dict: {} };
}

/** Best-effort: read the current test's filename from env (set by PW Test / Jest / Vitest). */
function currentTestFile() {
  // Playwright Test exposes test.info().titlePath but only inside a test;
  // a setup-time monkey-patch can't peek there. Fall back to a generic env
  // hint that wrappers set per-test, or null. Cache keys still work — they
  // include the constant name + intent which suffice in most cases.
  return process.env.QTEA_CURRENT_TEST_FILE || null;
}

/**
 * Resolve a sentinel against the live page using the tier ladder.
 * @param {any} page
 * @param {string} sentinel
 * @param {{skipDev?: boolean, skipCache?: boolean, skipHeuristic?: boolean}} opts
 * @returns {Promise<Resolution>}
 */
async function resolveSentinel(page, sentinel, opts) {
  if (devLocatorsCache === null) devLocatorsCache = loadDevLocators();
  const intent = parseSentinel(sentinel);
  const constantName = intent.slice(0, 64);  // intent doubles as constant fallback
  const testFile = currentTestFile();
  const skipDev = !!(opts && opts.skipDev);
  const skipCache = !!(opts && opts.skipCache);
  const skipHeuristic = !!(opts && opts.skipHeuristic);

  // Tier 1: dev-locators (keyed by intent for TS/JS — no easy way to walk
  // call stack for the constant name in JS without `Error().stack` parsing).
  if (!skipDev && devLocatorsCache && devLocatorsCache[constantName]) {
    const dev = devLocatorsCache[constantName];
    log("dev_locator_used", { constant: constantName, selector: dev.selector });
    return { selector: dev.selector, source: "dev", constantName, intent, testFile };
  }

  // Tier 2: runtime cache
  const cache = readCache();
  const key = cacheKey(testFile, constantName, intent);
  if (!skipCache) {
    const cached = cache[key];
    if (cached && cached.selector) {
      log("cache_hit", { constant: constantName, selector: cached.selector });
      const cachedCandidates = Array.isArray(cached.candidates) && cached.candidates.length
        ? cached.candidates
        : null;
      return {
        selector: cached.selector, source: "cached",
        constantName, intent, testFile,
        candidates: cachedCandidates,
      };
    }
  }

  // Capture AOM ONCE for tier 3 + 4
  const snap = await snapshotPage(page);

  // Tier 3: in-process heuristic
  if (!skipHeuristic) {
    const heur = heuristicResolve(intent, snap.dict);
    if (heur) {
      log("heuristic_hit", { constant: constantName, selector: heur });
      return { selector: heur, source: "heuristic", constantName, intent, testFile };
    }
  }

  // Tier 4: LLM via ResolverServer. Honour QTEA_NO_LLM_RESOLVE.
  if (process.env.QTEA_NO_LLM_RESOLVE === "1") {
    log("no_llm_resolve_active", { constant: constantName, intent });
    return { selector: null, source: "none", constantName, intent, testFile };
  }

  let pageUrl = null;
  try { pageUrl = typeof page.url === "function" ? page.url() : page.url; } catch (_) {}

  const result = await callResolverSocket({
    intent, constantName,
    snapshotText: snap.text, testFile, pageUrl,
  });
  if (!result || !result.selector) {
    log("resolver_failed", { constant: constantName, intent });
    return { selector: null, source: "none", constantName, intent, testFile };
  }
  const bundle = Array.isArray(result.candidates) && result.candidates.length
    ? result.candidates
    : null;
  log("resolver_ok", {
    constant: constantName, selector: result.selector,
    source: result.source, confidence: result.confidence,
    candidates: bundle ? bundle.length : 1,
  });
  return {
    selector: result.selector, source: "agent",
    constantName, intent, testFile,
    candidates: bundle,
  };
}

// ---------------------------------------------------------------------------
// RetryingLocator — ES6 Proxy wrapping the real Locator
// ---------------------------------------------------------------------------

// Action methods that can raise TimeoutError. Mirrors Python's _RETRIABLE_METHODS.
const RETRIABLE_METHODS = new Set([
  "click", "dblclick", "tap", "hover", "fill", "press", "pressSequentially", "type",
  "check", "uncheck", "setChecked", "setInputFiles",
  "selectOption", "selectText",
  "dragTo", "screenshot", "focus", "blur",
  "scrollIntoViewIfNeeded", "clear", "dispatchEvent",
  "waitFor", "textContent", "innerText", "innerHTML",
  "inputValue", "getAttribute", "evaluate", "evaluateHandle",
  "isVisible", "isHidden", "isEnabled", "isDisabled",
  "isChecked", "isEditable",
]);

function isPlaywrightTimeout(/** @type {any} */ err) {
  if (!err) return false;
  const name = err.name || (err.constructor && err.constructor.name);
  if (name === "TimeoutError") return true;
  const msg = String(err.message || err);
  return msg.includes("Timeout") && (msg.includes("exceeded") || msg.includes("ms exceeded"));
}

/**
 * Build a Proxy around a real Locator that, on TimeoutError, first walks
 * any remaining LLM-supplied fallback candidates from the resolution
 * bundle (zero LLM cost) and only invalidates + re-resolves when every
 * candidate in the bundle has been exhausted. On a fallback success, the
 * cache entry is rewritten with the working candidate as the sole entry
 * so the next test skips the failed primary entirely.
 *
 * @param {{real: any, page: any, sentinel: string, resolution: Resolution,
 *          originalLocator: (selector: string) => any}} ctx
 */
function makeRetryingLocator(ctx) {
  let real = ctx.real;
  let resolution = ctx.resolution;
  let retried = false;  // guards the LLM re-resolve to one attempt
  // candidates[0] is what's already wrapped in `real`; everything past it
  // is a fallback the retry path can try without a new resolver call.
  /** @type {Array<any>} */
  const remaining = (Array.isArray(resolution.candidates) && resolution.candidates.length > 1)
    ? resolution.candidates.slice(1)
    : [];
  const totalFallbacks = remaining.length;

  return new Proxy({}, {
    get(_target, prop) {
      const initialValue = real[prop];
      if (typeof initialValue !== "function" || !RETRIABLE_METHODS.has(String(prop)) || retried) {
        return typeof initialValue === "function" ? initialValue.bind(real) : initialValue;
      }
      return async (/** @type {any[]} */ ...args) => {
        // Walk any in-bundle fallbacks first (zero-cost resilience).
        while (true) {
          try {
            // Always read `real[prop]` afresh — `real` is swapped after
            // each candidate attempt and the new method must be picked up.
            const result = await real[prop].apply(real, args);
            // Success — if a fallback was used, promote it.
            if (remaining.length < totalFallbacks) {
              const usedIdx = totalFallbacks - remaining.length;  // 1..N
              const working = /** @type {any} */ (resolution.candidates)[usedIdx];
              const k = cacheKey(resolution.testFile, resolution.constantName, resolution.intent);
              promoteCandidateInCache(k, working);
            }
            return result;
          } catch (err) {
            if (!isPlaywrightTimeout(err)) throw err;
            log("retry_on_timeout", {
              constant: resolution.constantName,
              source: resolution.source, method: String(prop),
              remaining: remaining.length,
            });
            if (remaining.length > 0) {
              const nxt = remaining.shift();
              if (nxt && typeof nxt.selector === "string" && nxt.selector.trim()) {
                real = ctx.originalLocator(nxt.selector);
                log("fallback_candidate_try", {
                  constant: resolution.constantName,
                  selector: nxt.selector, strategy: nxt.strategy ?? null,
                });
                continue;
              }
            }
            // Bundle exhausted (or never existed) → LLM re-resolve.
            retried = true;
            if (resolution.source === "cached" || resolution.source === "agent") {
              const k = cacheKey(resolution.testFile, resolution.constantName, resolution.intent);
              invalidateCacheEntry(k);
            }
            const fresh = await resolveSentinel(ctx.page, ctx.sentinel, {
              skipDev: resolution.source === "dev",
              skipCache: true,
              skipHeuristic: resolution.source === "heuristic",
            });
            if (!fresh.selector) {
              log("retry_unresolvable", { constant: resolution.constantName });
              throw err;
            }
            real = ctx.originalLocator(fresh.selector);
            resolution = fresh;
            return await real[prop].apply(real, args);
          }
        }
      };
    },
  });
}

// ---------------------------------------------------------------------------
// Monkey-patches (install once, idempotent)
// ---------------------------------------------------------------------------

let installed = false;
const inflatedPages = new WeakSet();

function inflateTimeouts(/** @type {any} */ page) {
  if (process.env.QTEA_INFLATE_TIMEOUTS === "0") return;
  if (inflatedPages.has(page)) return;
  inflatedPages.add(page);
  const ms = parseInt(process.env.QTEA_DEFAULT_TIMEOUT_MS || "60000", 10) || 60000;
  try {
    if (typeof page.setDefaultTimeout === "function") page.setDefaultTimeout(ms);
  } catch (e) {
    log("timeout_inflate_skip", { error: String(e) });
  }
}

/**
 * Build a wrapper for {Page|Frame|Locator}.prototype.locator that intercepts
 * sentinel selectors. Returns the wrapper function.
 * @param {Function} original  the original `locator` method (bound at call time via `this`)
 * @param {"page" | "frame" | "locator"} kind
 */
function wrapLocatorMethod(original, kind) {
  return function wrappedLocator(/** @type {any} */ selector, /** @type {any} */ ...args) {
    if (kind === "page") inflateTimeouts(this);
    if (!isSentinel(selector)) {
      return original.call(this, selector, ...args);
    }
    // Sentinel path. Return a thenable proxy whose first action method
    // triggers async resolution. Playwright callers always `await` the
    // action, so this is transparent. The synchronous `.locator()` call
    // chains (e.g. `.first()`, `.nth(0)`) work because we lazy-resolve.
    const page = kind === "page" ? this : (this.page ? this.page() : this);
    const sentinel = selector;
    let resolution = /** @type {Resolution | null} */ (null);
    let real = /** @type {any} */ (null);

    const ensureReal = async () => {
      if (real) return;
      resolution = await resolveSentinel(page, sentinel, {});
      if (!resolution.selector) {
        throw new Error(
          `qtea JIT runtime: could not resolve locator ${JSON.stringify(parseSentinel(sentinel))}. ` +
          `See stderr for diagnostic.`
        );
      }
      real = original.call(this, resolution.selector, ...args);
    };

    // Return a Proxy that defers resolution until the first method call.
    const lazy = new Proxy({}, {
      get(_t, prop) {
        return async (/** @type {any[]} */ ...callArgs) => {
          await ensureReal();
          const inner = real[prop];
          if (typeof inner !== "function") return inner;
          // Once resolved, hand off to the retrying proxy for retriable methods.
          if (RETRIABLE_METHODS.has(String(prop))) {
            const wrapped = makeRetryingLocator({
              real, page, sentinel,
              // @ts-expect-error resolution narrowed above
              resolution,
              originalLocator: (sel) => original.call(this, sel, ...args),
            });
            return wrapped[prop](...callArgs);
          }
          return inner.apply(real, callArgs);
        };
      },
    });
    return lazy;
  };
}

function installMonkeyPatch() {
  if (installed) return;
  if (process.env.QTEA_DISABLE_JIT === "1") {
    log("disabled_via_env", {});
    return;
  }
  /** @type {any} */
  let pwCore;
  try {
    pwCore = require("playwright-core");
  } catch (e) {
    log("playwright_core_not_importable", { error: String(e) });
    return;
  }
  for (const cls of ["Page", "Frame", "Locator"]) {
    const klass = pwCore[cls];
    if (!klass || !klass.prototype || typeof klass.prototype.locator !== "function") continue;
    const original = klass.prototype.locator;
    klass.prototype.locator = wrapLocatorMethod(
      original,
      cls === "Page" ? "page" : cls === "Frame" ? "frame" : "locator",
    );
    log("locator_patched", { class: cls });
  }
  installed = true;
  log("installed", {});
}

// ---------------------------------------------------------------------------
// On-failure AOM capture — mirrors the Python `_capture_aom_on_failure`.
// Writes <QTEA_WORKSPACE_DIR>/aom-at-failure/<entry_id>.txt so the Step 9
// Layer 2 refinement can cross-check the failure against the live page
// AOM state.
//
// Registration: exported as `test` (an extension of @playwright/test's
// `test` with an auto-use fixture). Operators opt in with a one-line
// import change in their fixture module:
//
//     const { test, expect } = require("./tests/qtea-runtime");
//
// Without that change, the on-failure capture does NOT fire and Layer 2
// remains dead code for the JS SUT — no other behaviour is affected.
// ---------------------------------------------------------------------------

/** Match Python `md_parser.slugify`. */
function slugify(s) {
  const base = String(s || "").replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
  return base || "untitled";
}

/** Match Python `test_runner._normalize_id(file_rel, name)`. */
function normalizeTestId(fileRel, name) {
  const stem = fileRel ? path.parse(fileRel).name : "";
  const combined = fileRel ? `${stem}-${name}` : name;
  return "T-" + slugify(combined);
}

/**
 * Snapshot the page AOM and write it to
 * `<QTEA_WORKSPACE_DIR>/aom-at-failure/<entry_id>.txt`. Silently no-ops
 * when the workspace env-var is unset or the page is unusable — capture
 * failures must never affect the test outcome.
 * @param {any} page
 * @param {any} testInfo  Playwright Test's TestInfo (or a compatible shape)
 */
async function captureAomOnFailure(page, testInfo) {
  try {
    const workspace = process.env.QTEA_WORKSPACE_DIR;
    if (!workspace) return;
    if (!page || typeof page.locator !== "function") return;
    const snap = await snapshotPage(page);
    if (!snap || !snap.text) return;
    let fileRel = (testInfo && testInfo.file) ? String(testInfo.file) : "";
    if (fileRel) {
      try { fileRel = path.relative(process.cwd(), fileRel); } catch (_) {}
    }
    const title = (testInfo && testInfo.title) ? String(testInfo.title) : "test";
    const entryId = normalizeTestId(fileRel, title);
    const outDir = path.join(workspace, "aom-at-failure");
    fs.mkdirSync(outDir, { recursive: true });
    fs.writeFileSync(path.join(outDir, entryId + ".txt"), snap.text, "utf8");
    log("aom_capture_on_failure_ok", { entryId });
  } catch (e) {
    log("aom_capture_on_failure_failed", { error: String(e) });
  }
}

/**
 * Extend @playwright/test's `test` object with an auto-use fixture that
 * captures AOM on failed test teardown. Returns `null` when
 * `@playwright/test` is not installed (Jest/Mocha/Vitest SUTs) — the
 * caller should degrade gracefully.
 * @returns {any}
 */
function buildQteaTest() {
  let pwTest;
  try {
    pwTest = require("@playwright/test");
  } catch (_) {
    return null;
  }
  if (!pwTest || !pwTest.test || typeof pwTest.test.extend !== "function") {
    return null;
  }
  try {
    return pwTest.test.extend({
      // Auto-use fixture: no visible test API, no arg pollution. The
      // fixture body awaits `use()` (the test runs), then inspects
      // `testInfo.status` and captures AOM only on failure/timeout.
      _qteaFailureCapture: [
        async ({ page }, use, testInfo) => {
          await use(undefined);
          const status = testInfo && testInfo.status;
          if (status !== "failed" && status !== "timedOut") return;
          await captureAomOnFailure(page, testInfo);
        },
        { auto: true },
      ],
    });
  } catch (e) {
    log("qtea_test_extend_failed", { error: String(e) });
    return null;
  }
}

const _qteaTest = buildQteaTest();

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

// When loaded via Jest `setupFiles` / Vitest `setupFiles` / Playwright Test
// `globalSetup`, the import side-effect installs the patch.
installMonkeyPatch();

// Playwright Test's globalSetup expects a default-exported async function.
module.exports = async function globalSetup() {
  installMonkeyPatch();
};

module.exports.tbd = tbd;
module.exports.isSentinel = isSentinel;
module.exports.parseSentinel = parseSentinel;
module.exports.installMonkeyPatch = installMonkeyPatch;
// On-failure capture — operators opt in by importing `test`/`expect` from
// this module instead of `@playwright/test`.
module.exports.test = _qteaTest;
module.exports.expect = (() => {
  try { return require("@playwright/test").expect; } catch (_) { return null; }
})();
// Test-time exports (unit-tested in qtea's own suite).
module.exports.__internal = {
  heuristicResolve,
  parseIntent,
  cacheKey,
  ROLE_KEYWORDS,
  NAME_FILLERS,
  SENTINEL_PREFIX,
  // AOM helpers (Thread 2: modern API + iframe support).
  AOM_CAPS,
  readAomEnv,
  aomKwargLadder,
  callAriaSnapshot,
  iframeLabel,
  appendIframeSnapshots,
  parseAriaSnapshotYaml,
  snapshotPage,
  // On-failure capture (Thread 2 sub-task).
  slugify,
  normalizeTestId,
  captureAomOnFailure,
};
