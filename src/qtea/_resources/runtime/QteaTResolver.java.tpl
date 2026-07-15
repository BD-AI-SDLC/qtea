package com.qtea.runtime;

import com.microsoft.playwright.Page;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;

/**
 * Resolution chain for the Java JIT runtime. Tiers mirror the Python and
 * TypeScript runtimes:
 *
 * <ol>
 *   <li>Dev-locators file (zero LLM)</li>
 *   <li>Runtime cache (zero LLM)</li>
 *   <li>In-process heuristic (zero LLM — exact role+name match in the AOM)</li>
 *   <li>LLM via parent ResolverServer over loopback TCP (one LLM call per cold miss)</li>
 *   <li>Unresolvable — caller raises a clear runtime exception</li>
 * </ol>
 *
 * <p>Self-contained — only standard JDK + Playwright-Java on the classpath.
 * No JSON-binding dependency: we use a tiny in-file JSON writer/parser
 * because the wire-format is a fixed shape and pulling in Jackson/Gson
 * would inflate the SUT's build for one method call.
 */
final class QteaTResolver {

    static final class Candidate {
        final String selector;
        final String strategy;     // nullable
        final Double confidence;   // nullable
        final String reason;       // nullable

        Candidate(String selector, String strategy, Double confidence, String reason) {
            this.selector = selector;
            this.strategy = strategy;
            this.confidence = confidence;
            this.reason = reason;
        }
    }

    static final class Resolution {
        final String selector;
        final String source;          // "dev" | "cached" | "heuristic" | "agent" | "none"
        final String constantName;
        final String intent;
        final String testFile;
        /** Ranked candidate bundle (primary + optional fallback). null for
         *  non-LLM sources or when the bundle is unknown (e.g. disk cache
         *  hit across JVM restarts — see {@link #bundleCache}). */
        final List<Candidate> candidates;

        Resolution(String selector, String source, String constantName,
                   String intent, String testFile, List<Candidate> candidates) {
            this.selector = selector;
            this.source = source;
            this.constantName = constantName;
            this.intent = intent;
            this.testFile = testFile;
            this.candidates = candidates;
        }

        Resolution(String selector, String source, String constantName,
                   String intent, String testFile) {
            this(selector, source, constantName, intent, testFile, null);
        }
    }

    /**
     * In-memory cross-test bundle store keyed by cache key. The disk cache
     * format (regex-based, single-selector-per-entry) intentionally does
     * NOT persist the candidates array — adding full JSON-bundle round-trip
     * to the regex parser would be unsafe. Instead, the JVM-scoped map
     * carries bundles across tests within a single Maven/Gradle invocation
     * (the common case); cross-JVM-restart, bundles are absent and the
     * cache hit degrades to single-candidate behaviour (= pre-bundle world).
     */
    private static final Map<String, List<Candidate>> bundleCache = new ConcurrentHashMap<>();

    private QteaTResolver() {}

    // ----------------------------------------------------------------------
    // Public entry point
    // ----------------------------------------------------------------------

    static Resolution resolveSentinel(
        Page page, String sentinel,
        boolean skipDev, boolean skipCache, boolean skipHeuristic
    ) {
        String intent = Tbd.parseSentinel(sentinel);
        String constantName = intent.substring(0, Math.min(intent.length(), 64));
        String testFile = System.getProperty("qtea.current_test_file");
        if (testFile == null) testFile = System.getenv("QTEA_CURRENT_TEST_FILE");

        // Tier 1: dev-locators
        if (!skipDev) {
            String dev = devLocator(constantName);
            if (dev != null) {
                log("dev_locator_used", "constant", constantName, "selector", dev);
                return new Resolution(dev, "dev", constantName, intent, testFile);
            }
        }

        // Tier 2: cache
        String key = cacheKey(testFile, constantName, intent);
        if (!skipCache) {
            String cached = cacheLookup(key);
            if (cached != null) {
                log("cache_hit", "constant", constantName, "selector", cached);
                // Pair the disk-cached selector with any in-memory bundle for
                // this key (populated by an earlier LLM resolve in the same
                // JVM run). Absent across cold JVM starts — fine, the retry
                // proxy degrades to single-candidate behaviour.
                List<Candidate> cachedBundle = bundleCache.get(key);
                return new Resolution(cached, "cached", constantName, intent, testFile, cachedBundle);
            }
        }

        // Capture AOM once for tier 3 + 4
        String snapshotText = snapshotPage(page);

        // Tier 3: heuristic
        if (!skipHeuristic) {
            String heur = heuristicResolve(intent, snapshotText);
            if (heur != null) {
                log("heuristic_hit", "constant", constantName, "selector", heur);
                return new Resolution(heur, "heuristic", constantName, intent, testFile);
            }
        }

        // Tier 4: LLM via ResolverServer
        if ("1".equals(System.getenv("QTEA_NO_LLM_RESOLVE"))) {
            log("no_llm_resolve_active", "constant", constantName, "intent", intent);
            return new Resolution(null, "none", constantName, intent, testFile);
        }
        ResolverResponse rr = callResolverServer(intent, constantName, snapshotText, testFile, safePageUrl(page));
        if (rr == null || rr.selector == null) {
            log("resolver_failed", "constant", constantName, "intent", intent);
            return new Resolution(null, "none", constantName, intent, testFile);
        }
        log("resolver_ok", "constant", constantName, "selector", rr.selector);
        // Cache the bundle in-memory so subsequent tests in the same JVM hit
        // it via tier 2 and can also walk the fallback candidate.
        if (rr.candidates != null && !rr.candidates.isEmpty()) {
            bundleCache.put(key, rr.candidates);
        }
        return new Resolution(rr.selector, "agent", constantName, intent, testFile, rr.candidates);
    }

    /**
     * Rewrite the disk cache entry's primary selector to the working fallback
     * AND update the in-memory bundle cache so the working candidate is the
     * sole entry. Called after a fallback candidate survives an action that
     * timed out under the original primary.
     */
    static void promoteCandidateInCache(String testFile, String constantName, String intent, Candidate working) {
        String key = cacheKey(testFile, constantName, intent);
        Path p = cachePath();
        if (p != null) {
            try {
                Map<String, String> entries = readCacheEntries(p);
                if (entries.containsKey(key)) {
                    entries.put(key, working.selector);
                    writeCacheEntries(p, entries);
                }
            } catch (Exception e) {
                log("fallback_promote_failed", "error", e.toString());
            }
        }
        bundleCache.put(key, Collections.singletonList(working));
        log("fallback_promoted", "key", key, "selector", working.selector);
    }

    static void invalidateCacheEntry(String testFile, String constantName, String intent) {
        String key = cacheKey(testFile, constantName, intent);
        Path p = cachePath();
        if (p == null || !Files.exists(p)) return;
        try {
            Map<String, String> entries = readCacheEntries(p);
            if (entries.remove(key) != null) {
                writeCacheEntries(p, entries);
                log("cache_invalidated", "key", key);
            }
        } catch (Exception e) {
            log("cache_invalidate_failed", "error", e.toString());
        }
    }

    static boolean isPlaywrightTimeout(Throwable t) {
        if (t == null) return false;
        if (t.getClass().getName().endsWith(".TimeoutError")) return true;
        String msg = t.getMessage() == null ? "" : t.getMessage();
        return msg.contains("Timeout") && (msg.contains("exceeded") || msg.contains("ms exceeded"));
    }

    // ----------------------------------------------------------------------
    // Snapshot — capability-probing Locator.ariaSnapshot ladder + iframe enum
    // ----------------------------------------------------------------------
    //
    // Mirrors the Python + JS runtime designs. Capability ladder, probed
    // via reflection once per JVM and cached in volatile Booleans:
    //
    //   Rung A (Playwright Java 1.60+): ariaSnapshot({mode:AI, boxes:true})
    //   Rung B (Playwright Java 1.59):  ariaSnapshot({mode:AI})
    //   Rung C (Playwright Java 1.49-1.58): ariaSnapshot() (no opts;
    //           iframes NOT included by Playwright — enumerate page.frames()
    //           manually and append with `# iframe: <label>` markers)
    //   Rung D (Playwright Java <1.49): legacy page.accessibility().snapshot()
    //           (removed in 1.57; gated by QTEA_AOM_LEGACY_OK)

    /** Capability cache: null=unprobed, true/false=proven supported/not. */
    private static volatile Boolean AOM_MODE_AI = null;
    private static volatile Boolean AOM_BOXES = null;

    private enum AomBoxesEnv { AUTO, OFF, FORCE }

    private static AomBoxesEnv readBoxesEnv() {
        String v = System.getenv("QTEA_AOM_BOXES");
        if (v == null) return AomBoxesEnv.AUTO;
        String lower = v.trim().toLowerCase();
        if (lower.equals("off")) return AomBoxesEnv.OFF;
        if (lower.equals("force")) return AomBoxesEnv.FORCE;
        return AomBoxesEnv.AUTO;
    }

    private static Integer readDepthEnv() {
        String s = System.getenv("QTEA_AOM_DEPTH");
        if (s == null || s.isEmpty()) return null;
        try { int n = Integer.parseInt(s.trim()); return n > 0 ? n : null; }
        catch (NumberFormatException e) { return null; }
    }

    private static boolean readLegacyOkEnv() {
        return !"0".equals(System.getenv("QTEA_AOM_LEGACY_OK"));
    }

    /**
     * Package-external accessor for the on-failure capture path in
     * {@link QteaT#captureAomOnFailure(Page, String)}. Returns the same
     * text {@link #snapshotPage(Page)} produces internally for tier 3/4
     * resolution — modern ariaSnapshot with iframe enum when supported,
     * legacy accessibility JSON otherwise. Never throws.
     */
    static String snapshotPageForCapture(Page page) {
        try { return snapshotPage(page); }
        catch (Throwable t) {
            log("snapshot_for_capture_failed", "error", t.toString());
            return "";
        }
    }

    private static String snapshotPage(Page page) {
        AomBoxesEnv boxes = readBoxesEnv();
        Integer depth = readDepthEnv();
        // ---- Primary: Locator.ariaSnapshot (Playwright Java 1.49+) ----
        String text = tryAriaSnapshot(page, depth, boxes);
        if (text != null) {
            // Rungs A/B include iframe subtrees automatically via mode='ai'.
            // Rung C does NOT — enumerate manually.
            if (Boolean.TRUE.equals(AOM_MODE_AI)) return text;
            return appendIframeSnapshots(page, text, depth, boxes);
        }

        // ---- Legacy: page.accessibility().snapshot() (Playwright Java <1.49
        //      or old versions where ariaSnapshot is unavailable) ----
        if (!readLegacyOkEnv()) return "";
        try {
            Object accessibility = Page.class.getMethod("accessibility").invoke(page);
            Object snapshot = accessibility.getClass().getMethod("snapshot").invoke(accessibility);
            if (snapshot == null) return "{}";
            return jsonStringifyAom(snapshot);
        } catch (Throwable t) {
            log("snapshot_failed", "error", t.toString());
            return "";
        }
    }

    /** Try Locator.ariaSnapshot on page.locator("body") via reflection.
     *  Returns null if the method itself is missing (older Playwright),
     *  signalling the caller to fall through to the legacy path. */
    private static String tryAriaSnapshot(Page page, Integer depth, AomBoxesEnv boxes) {
        Object body;
        try { body = page.locator("body"); }
        catch (Throwable t) { return null; }
        if (body == null) return null;
        return tryAriaSnapshotOnLocator(body, depth, boxes);
    }

    /** Walk the kwarg ladder against an already-obtained body Locator.
     *  Used both for main-frame snapshotting and per-iframe enumeration. */
    private static String tryAriaSnapshotOnLocator(Object body, Integer depth, AomBoxesEnv boxes) {
        Class<?> optsClass;
        try { optsClass = Class.forName("com.microsoft.playwright.Locator$AriaSnapshotOptions"); }
        catch (ClassNotFoundException e) { optsClass = null; }
        java.lang.reflect.Method mWithOpts = null;
        if (optsClass != null) {
            try { mWithOpts = body.getClass().getMethod("ariaSnapshot", optsClass); }
            catch (NoSuchMethodException e) { mWithOpts = null; }
        }
        java.lang.reflect.Method mNoArgs;
        try { mNoArgs = body.getClass().getMethod("ariaSnapshot"); }
        catch (NoSuchMethodException e) { mNoArgs = null; }
        // If neither shape exists, method is genuinely absent — caller falls
        // through to legacy.
        if (mWithOpts == null && mNoArgs == null) return null;

        Throwable lastErr = null;

        // Rung A: mode='ai' + boxes=true
        if (mWithOpts != null
                && boxes != AomBoxesEnv.OFF
                && !Boolean.FALSE.equals(AOM_MODE_AI)
                && (boxes == AomBoxesEnv.FORCE || !Boolean.FALSE.equals(AOM_BOXES))) {
            Object opts = buildOpts(optsClass, "AI", Boolean.TRUE, depth);
            if (opts != null) {
                try {
                    Object result = mWithOpts.invoke(body, opts);
                    AOM_MODE_AI = true;
                    AOM_BOXES = true;
                    return result == null ? "" : result.toString();
                } catch (Throwable t) {
                    Throwable cause = unwrapReflection(t);
                    if (isSignatureError(cause)) {
                        AOM_BOXES = false;
                        lastErr = t;
                    } else {
                        lastErr = t;
                    }
                }
            }
        }

        // Rung B: mode='ai' (no boxes)
        if (mWithOpts != null && !Boolean.FALSE.equals(AOM_MODE_AI)) {
            Object opts = buildOpts(optsClass, "AI", null, depth);
            if (opts != null) {
                try {
                    Object result = mWithOpts.invoke(body, opts);
                    AOM_MODE_AI = true;
                    return result == null ? "" : result.toString();
                } catch (Throwable t) {
                    Throwable cause = unwrapReflection(t);
                    if (isSignatureError(cause)) {
                        AOM_MODE_AI = false;
                        lastErr = t;
                    } else {
                        lastErr = t;
                    }
                }
            }
        }

        // Rung C: no opts
        if (mNoArgs != null) {
            try {
                Object result = mNoArgs.invoke(body);
                return result == null ? "" : result.toString();
            } catch (Throwable t) {
                lastErr = t;
            }
        }

        if (lastErr != null) log("snapshot_failed_aria", "error", lastErr.toString());
        return null;
    }

    /** Build an AriaSnapshotOptions instance via reflection. Returns null
     *  when a required setter is missing (older Playwright missing
     *  setMode/setBoxes altogether). */
    private static Object buildOpts(Class<?> optsClass, String modeName, Boolean boxes, Integer depth) {
        if (optsClass == null) return null;
        Object opts;
        try {
            opts = optsClass.getDeclaredConstructor().newInstance();
        } catch (Throwable t) {
            return null;
        }
        if (modeName != null) {
            boolean modeSet = false;
            // Try AriaSnapshotMode enum first (typical Playwright Java shape).
            try {
                Class<?> modeClass = Class.forName("com.microsoft.playwright.options.AriaSnapshotMode");
                @SuppressWarnings({"unchecked", "rawtypes"})
                Object modeValue = Enum.valueOf((Class<? extends Enum>) modeClass, modeName);
                optsClass.getMethod("setMode", modeClass).invoke(opts, modeValue);
                modeSet = true;
            } catch (Throwable ignored) {}
            if (!modeSet) {
                try {
                    optsClass.getMethod("setMode", String.class).invoke(opts, modeName);
                    modeSet = true;
                } catch (Throwable ignored) {}
            }
            if (!modeSet) return null;
        }
        if (boxes != null) {
            try {
                optsClass.getMethod("setBoxes", boolean.class).invoke(opts, boxes.booleanValue());
            } catch (Throwable e) {
                if (boxes.booleanValue()) return null;  // requested but setter missing
            }
        }
        if (depth != null) {
            try {
                optsClass.getMethod("setDepth", int.class).invoke(opts, depth.intValue());
            } catch (Throwable ignored) {
                // depth is best-effort — silently drop when unavailable.
            }
        }
        return opts;
    }

    /** Enumerate non-main-frames and append each frame's AOM snapshot to
     *  {@code mainText} with a `# iframe: <label>` marker. Silently skips
     *  frames whose snapshot raises. */
    private static String appendIframeSnapshots(Page page, String mainText, Integer depth, AomBoxesEnv boxes) {
        StringBuilder sb = new StringBuilder(mainText);
        List<?> frames;
        try {
            frames = (List<?>) Page.class.getMethod("frames").invoke(page);
        } catch (Throwable t) {
            return mainText;
        }
        if (frames == null) return mainText;
        Object mainFrame = null;
        try {
            mainFrame = Page.class.getMethod("mainFrame").invoke(page);
        } catch (Throwable ignored) {}
        for (Object frame : frames) {
            if (frame == mainFrame) continue;
            try {
                Object body = frame.getClass().getMethod("locator", String.class).invoke(frame, "body");
                if (body == null) continue;
                String sub = tryAriaSnapshotOnLocator(body, depth, boxes);
                if (sub == null || sub.isEmpty()) continue;
                sb.append("\n# iframe: ").append(iframeLabel(frame)).append("\n").append(sub);
            } catch (Throwable t) {
                log("iframe_snapshot_skip", "error", t.toString());
            }
        }
        return sb.toString();
    }

    /** Best-effort iframe label: url() → name() → "unknown". */
    private static String iframeLabel(Object frame) {
        try {
            Object url = frame.getClass().getMethod("url").invoke(frame);
            if (url != null) {
                String s = url.toString();
                if (!s.isEmpty()) return s;
            }
        } catch (Throwable ignored) {}
        try {
            Object name = frame.getClass().getMethod("name").invoke(frame);
            if (name != null) {
                String s = name.toString();
                if (!s.isEmpty()) return s;
            }
        } catch (Throwable ignored) {}
        return "unknown";
    }

    /** Unwrap InvocationTargetException — the real cause is in getCause(). */
    private static Throwable unwrapReflection(Throwable t) {
        if (t == null) return null;
        if (t instanceof java.lang.reflect.InvocationTargetException && t.getCause() != null) {
            return t.getCause();
        }
        return t;
    }

    /** Heuristic detection of a Playwright signature-mismatch error.
     *  Playwright Java raises IllegalArgumentException or PlaywrightException
     *  with schema-related message text on unknown options; older versions
     *  raise NoSuchMethodException from the reflection layer directly. */
    private static boolean isSignatureError(Throwable t) {
        if (t == null) return false;
        if (t instanceof NoSuchMethodException) return true;
        String cls = t.getClass().getName();
        if (cls.endsWith(".IllegalArgumentException")) return true;
        String msg = t.getMessage() == null ? "" : t.getMessage().toLowerCase();
        return msg.contains("unknown") || msg.contains("unexpected")
            || msg.contains("invalid") || msg.contains("no such")
            || msg.contains("unrecognized");
    }

    private static String safePageUrl(Page page) {
        try { return page.url(); } catch (Throwable t) { return null; }
    }

    /** Best-effort AOM serializer — Playwright-Java returns
     *  {@code AccessibilitySnapshotResult} POJOs; we walk via reflection. */
    private static String jsonStringifyAom(Object node) {
        StringBuilder sb = new StringBuilder();
        appendAomNode(sb, node, 0);
        return sb.toString();
    }

    private static void appendAomNode(StringBuilder sb, Object node, int depth) {
        if (node == null || depth > 50) { sb.append("null"); return; }
        sb.append("{");
        sb.append("\"role\":").append(jsonString(getField(node, "role")));
        sb.append(",\"name\":").append(jsonString(getField(node, "name")));
        Object children = getFieldRaw(node, "children");
        sb.append(",\"children\":[");
        if (children instanceof Iterable<?>) {
            boolean first = true;
            for (Object child : (Iterable<?>) children) {
                if (!first) sb.append(",");
                appendAomNode(sb, child, depth + 1);
                first = false;
            }
        }
        sb.append("]}");
    }

    private static String getField(Object obj, String name) {
        Object v = getFieldRaw(obj, name);
        return v == null ? null : v.toString();
    }

    private static Object getFieldRaw(Object obj, String name) {
        if (obj == null) return null;
        try {
            java.lang.reflect.Field f = obj.getClass().getDeclaredField(name);
            f.setAccessible(true);
            return f.get(obj);
        } catch (Throwable t) {
            return null;
        }
    }

    // ----------------------------------------------------------------------
    // Tier-3 heuristic — port of the Python implementation
    // ----------------------------------------------------------------------

    private static final Map<String, String> ROLE_KEYWORDS = buildRoleKeywords();
    private static final Set<String> NAME_FILLERS = Set.of(
        "the", "a", "an", "on", "in", "of", "for", "to", "with", "by",
        "primary", "main", "secondary"
    );
    private static final double HEURISTIC_MIN_SCORE = 0.9;
    private static final double HEURISTIC_TIE_GAP = 0.1;

    private static Map<String, String> buildRoleKeywords() {
        Map<String, String> m = new LinkedHashMap<>();
        for (String[] kv : new String[][]{
            {"button", "button"}, {"submit", "button"}, {"btn", "button"},
            {"link", "link"}, {"anchor", "link"},
            {"tab", "tab"},
            {"input", "textbox"}, {"field", "textbox"}, {"textbox", "textbox"}, {"textfield", "textbox"},
            {"checkbox", "checkbox"},
            {"radio", "radio"},
            {"dropdown", "combobox"}, {"select", "combobox"}, {"combobox", "combobox"},
            {"menu", "menu"}, {"menuitem", "menuitem"},
            {"heading", "heading"}, {"title", "heading"}, {"header", "heading"},
            {"image", "img"}, {"icon", "img"}, {"img", "img"},
            {"form", "form"},
            {"dialog", "dialog"}, {"modal", "dialog"},
            {"alert", "alert"}, {"banner", "banner"},
            {"list", "list"}, {"listitem", "listitem"},
            {"row", "row"}, {"cell", "cell"}, {"columnheader", "columnheader"},
            {"tooltip", "tooltip"},
            {"tree", "tree"}, {"treeitem", "treeitem"},
            {"switch", "switch"}, {"toggle", "switch"},
            {"slider", "slider"},
            {"spinbutton", "spinbutton"},
            {"search", "search"}, {"searchbox", "searchbox"},
            {"navigation", "navigation"}, {"nav", "navigation"},
        }) {
            m.put(kv[0], kv[1]);
        }
        return Collections.unmodifiableMap(m);
    }

    static String heuristicResolve(String intent, String snapshotJson) {
        if (snapshotJson == null || snapshotJson.isEmpty() || "{}".equals(snapshotJson.trim())) return null;
        String[] tokens = intent.toLowerCase().split("\\W+");
        String role = null;
        List<String> nameTokens = new ArrayList<>();
        for (String t : tokens) {
            if (t.isEmpty()) continue;
            if (role == null && ROLE_KEYWORDS.containsKey(t)) { role = ROLE_KEYWORDS.get(t); continue; }
            if (NAME_FILLERS.contains(t)) continue;
            if (ROLE_KEYWORDS.containsKey(t)) continue;
            nameTokens.add(t);
        }
        if (role == null || nameTokens.isEmpty()) return null;
        String nameHint = String.join(" ", nameTokens);

        // Tiny streaming walk over the JSON we just generated. We avoid a
        // full JSON parser by leveraging the known shape (we wrote it).
        List<double[]> scoresIndex = new ArrayList<>();
        List<String> winners = new ArrayList<>();
        scanAomForRole(snapshotJson, role, nameHint, nameTokens, scoresIndex, winners);
        if (winners.isEmpty()) return null;
        // Build (score,name) pairs and sort
        Integer[] order = new Integer[winners.size()];
        for (int i = 0; i < order.length; i++) order[i] = i;
        Arrays.sort(order, (a, b) -> Double.compare(scoresIndex.get(b)[0], scoresIndex.get(a)[0]));
        double top = scoresIndex.get(order[0])[0];
        if (top < HEURISTIC_MIN_SCORE) return null;
        if (winners.size() > 1 && top - scoresIndex.get(order[1])[0] < HEURISTIC_TIE_GAP) return null;
        return formatRoleSelector(role, winners.get(order[0]));
    }

    private static final Pattern ROLE_NAME_PATTERN = Pattern.compile(
        "\"role\":\"([^\"]*)\",\"name\":(null|\"((?:\\\\.|[^\"\\\\])*)\")"
    );

    /** Walks `{"role":"x","name":"y", ...}` shapes in our own JSON output. */
    private static void scanAomForRole(String json, String role, String nameHint,
                                        List<String> nameTokens,
                                        List<double[]> scoresOut, List<String> winnersOut) {
        java.util.regex.Matcher m = ROLE_NAME_PATTERN.matcher(json);
        while (m.find()) {
            String nodeRole = m.group(1);
            if (!role.equals(nodeRole)) continue;
            String nameLit = m.group(2);
            if ("null".equals(nameLit)) continue;
            String nodeName = unescapeJson(m.group(3));
            String lower = nodeName.toLowerCase();
            if (lower.isEmpty()) continue;
            if (lower.contains(nameHint)) {
                scoresOut.add(new double[]{1.0});
                winnersOut.add(nodeName);
            } else if (nameTokens.stream().allMatch(lower::contains)) {
                scoresOut.add(new double[]{0.95});
                winnersOut.add(nodeName);
            } else if (nameTokens.stream().anyMatch(lower::contains)) {
                scoresOut.add(new double[]{0.6});
                winnersOut.add(nodeName);
            }
        }
    }

    private static String formatRoleSelector(String role, String name) {
        String escaped = name.replace("\\", "\\\\").replace("\"", "\\\"");
        return "role=" + role + "[name=\"" + escaped + "\"]";
    }

    // ----------------------------------------------------------------------
    // Dev-locators + cache
    // ----------------------------------------------------------------------

    private static volatile Map<String, String> devLocatorsCache;

    private static String devLocator(String constantName) {
        if (devLocatorsCache == null) {
            synchronized (QteaTResolver.class) {
                if (devLocatorsCache == null) devLocatorsCache = loadDevLocators();
            }
        }
        return devLocatorsCache.get(constantName);
    }

    private static Map<String, String> loadDevLocators() {
        List<Path> candidates = new ArrayList<>();
        String env = System.getenv("QTEA_DEV_LOCATORS");
        if (env != null) candidates.add(Paths.get(env));
        candidates.add(Paths.get("").toAbsolutePath().resolve(".qtea/dev-locators.json"));
        for (Path p : candidates) {
            if (!Files.isRegularFile(p)) continue;
            try {
                String raw = new String(Files.readAllBytes(p), StandardCharsets.UTF_8);
                Map<String, String> out = parseDevLocators(raw);
                log("dev_locators_loaded", "path", p.toString(), "count", String.valueOf(out.size()));
                return out;
            } catch (IOException e) {
                continue;
            }
        }
        return new HashMap<>();
    }

    /** Minimal parse of {"locators": {"NAME": {"selector": "..."}}, ...}. */
    private static Map<String, String> parseDevLocators(String raw) {
        Map<String, String> out = new HashMap<>();
        // crude: find "locators" object; iterate top-level keys
        int locIdx = raw.indexOf("\"locators\"");
        if (locIdx < 0) return out;
        int braceStart = raw.indexOf('{', locIdx);
        if (braceStart < 0) return out;
        Pattern entryRe = Pattern.compile(
            "\"([A-Za-z_][A-Za-z0-9_]*)\"\\s*:\\s*\\{[^}]*\"selector\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\""
        );
        java.util.regex.Matcher m = entryRe.matcher(raw.substring(braceStart));
        while (m.find()) {
            String sel = unescapeJson(m.group(2));
            if (sel.startsWith("//") || sel.startsWith("xpath=") || sel.contains("By.XPATH")) continue;
            out.put(m.group(1), sel);
        }
        return out;
    }

    private static Path cachePath() {
        String dir = System.getenv("QTEA_CACHE_DIR");
        if (dir == null || dir.isEmpty()) return null;
        return Paths.get(dir, "locator-cache.json");
    }

    private static Map<String, String> readCacheEntries(Path p) throws IOException {
        Map<String, String> out = new LinkedHashMap<>();
        if (!Files.isRegularFile(p)) return out;
        String raw = new String(Files.readAllBytes(p), StandardCharsets.UTF_8);
        // crude entry parser: looks for {"key":"...","selector":"..."}
        Pattern entryRe = Pattern.compile(
            "\\{[^{}]*\"key\"\\s*:\\s*\"([0-9a-f]+)\"[^{}]*\"selector\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\""
        );
        java.util.regex.Matcher m = entryRe.matcher(raw);
        while (m.find()) {
            out.put(m.group(1), unescapeJson(m.group(2)));
        }
        return out;
    }

    private static void writeCacheEntries(Path p, Map<String, String> entries) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"entries\":[");
        boolean first = true;
        for (Map.Entry<String, String> e : entries.entrySet()) {
            if (!first) sb.append(",");
            sb.append("{\"key\":\"").append(e.getKey()).append("\",")
              .append("\"selector\":\"").append(escapeJson(e.getValue())).append("\"}");
            first = false;
        }
        sb.append("]}");
        Path tmp = Paths.get(p.toString() + ".tmp");
        Files.write(tmp, sb.toString().getBytes(StandardCharsets.UTF_8));
        Files.move(tmp, p, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
    }

    private static String cacheLookup(String key) {
        Path p = cachePath();
        if (p == null) return null;
        try {
            return readCacheEntries(p).get(key);
        } catch (IOException e) {
            return null;
        }
    }

    static String cacheKey(String testFile, String constantName, String intent) {
        String norm = (intent == null ? "" : intent).trim().toLowerCase().replaceAll("\\s+", " ");
        String payload = (testFile == null ? "" : testFile) + "::" + constantName + "::" + norm;
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] hash = md.digest(payload.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 8; i++) sb.append(String.format("%02x", hash[i]));
            return sb.toString();
        } catch (Exception e) {
            return Integer.toHexString(payload.hashCode());
        }
    }

    // ----------------------------------------------------------------------
    // Tier-4 LLM via ResolverServer (TCP loopback)
    // ----------------------------------------------------------------------

    /** Compact response carrier so we can hand back both the primary selector
     *  AND its candidates bundle without two parsing passes. */
    private static final class ResolverResponse {
        final String selector;            // nullable
        final List<Candidate> candidates; // nullable / possibly empty

        ResolverResponse(String selector, List<Candidate> candidates) {
            this.selector = selector;
            this.candidates = candidates;
        }
    }

    private static ResolverResponse callResolverServer(
        String intent, String constantName, String snapshotText,
        String testFile, String pageUrl
    ) {
        String portStr = System.getenv("QTEA_RESOLVER_PORT");
        String token = System.getenv("QTEA_RESOLVER_TOKEN");
        if (portStr == null || token == null) {
            log("resolver_socket_no_env", "hint",
                "QTEA_RESOLVER_PORT/TOKEN not set; resolver disabled");
            return null;
        }
        int port;
        try { port = Integer.parseInt(portStr); }
        catch (NumberFormatException e) { return null; }
        try (Socket sock = new Socket()) {
            sock.connect(new InetSocketAddress("127.0.0.1", port), 5000);
            sock.setSoTimeout(180_000);
            String req = "{"
                + "\"token\":\"" + escapeJson(token) + "\","
                + "\"intent\":\"" + escapeJson(intent) + "\","
                + "\"constant_name\":\"" + escapeJson(constantName) + "\","
                + "\"snapshot_text\":\"" + escapeJson(snapshotText) + "\","
                + "\"test_file\":" + jsonString(testFile) + ","
                + "\"page_url\":" + jsonString(pageUrl) + ","
                + "\"source_type\":\"aom\""
                + "}\n";
            OutputStream os = sock.getOutputStream();
            os.write(req.getBytes(StandardCharsets.UTF_8));
            os.flush();
            BufferedReader br = new BufferedReader(
                new InputStreamReader(sock.getInputStream(), StandardCharsets.UTF_8)
            );
            String line = br.readLine();
            if (line == null) return null;
            return parseResolverResponse(line);
        } catch (IOException e) {
            log("resolver_socket_error", "error", e.toString());
            return null;
        }
    }

    private static ResolverResponse parseResolverResponse(String responseLine) {
        if (responseLine.contains("\"ok\":false")) return null;
        // Top-level selector first (the "winner" the server already mirrored
        // from candidates[0]). Then walk the candidates array, if present.
        String topSelector = extractTopLevelString(responseLine, "selector");
        if (topSelector != null && topSelector.isEmpty()) topSelector = null;
        List<Candidate> bundle = parseCandidatesArray(responseLine);
        return new ResolverResponse(topSelector, bundle);
    }

    /** Match a top-level (not nested) ``"key":"value"`` in a JSON response.
     *  The {@code candidates} array is shallow (each entry is a flat object)
     *  so the first ``"selector":...`` match outside the bracketed array is
     *  the top-level field. We use a simple heuristic: prefer the match that
     *  occurs BEFORE the candidates bracket; fall back to the first match. */
    private static String extractTopLevelString(String json, String key) {
        int candIdx = json.indexOf("\"candidates\"");
        String scope = candIdx > 0 ? json.substring(0, candIdx) : json;
        Pattern p = Pattern.compile("\"" + Pattern.quote(key) + "\"\\s*:\\s*(null|\"((?:\\\\.|[^\"\\\\])*)\")");
        java.util.regex.Matcher m = p.matcher(scope);
        if (m.find()) {
            if ("null".equals(m.group(1))) return null;
            return unescapeJson(m.group(2));
        }
        // Fall back to whole-doc scan (server may have re-ordered fields).
        m = p.matcher(json);
        if (m.find()) {
            if ("null".equals(m.group(1))) return null;
            return unescapeJson(m.group(2));
        }
        return null;
    }

    /** Extract the {@code candidates} array from a resolver response. Each
     *  candidate object is parsed flat (no nested objects). Returns an empty
     *  list when the field is missing or empty; never returns null. */
    private static List<Candidate> parseCandidatesArray(String json) {
        int idx = json.indexOf("\"candidates\"");
        if (idx < 0) return Collections.emptyList();
        int colon = json.indexOf(':', idx);
        if (colon < 0) return Collections.emptyList();
        int arrStart = -1;
        for (int i = colon + 1; i < json.length(); i++) {
            char c = json.charAt(i);
            if (Character.isWhitespace(c)) continue;
            if (c == '[') { arrStart = i; break; }
            if (c == 'n') return Collections.emptyList();  // null
            break;
        }
        if (arrStart < 0) return Collections.emptyList();
        int arrEnd = findBalancedJson(json, arrStart, '[', ']');
        if (arrEnd < 0) return Collections.emptyList();
        String body = json.substring(arrStart + 1, arrEnd);
        List<Candidate> out = new ArrayList<>();
        int p = 0;
        while (p < body.length()) {
            int objStart = body.indexOf('{', p);
            if (objStart < 0) break;
            int objEnd = findBalancedJson(body, objStart, '{', '}');
            if (objEnd < 0) break;
            Candidate c = parseCandidateObject(body.substring(objStart, objEnd + 1));
            if (c != null) out.add(c);
            p = objEnd + 1;
        }
        return out;
    }

    private static Candidate parseCandidateObject(String obj) {
        String selector = extractTopLevelString(obj, "selector");
        if (selector == null || selector.isEmpty()) return null;
        if (selector.startsWith("//") || selector.startsWith("xpath=") || selector.contains("By.XPATH")) {
            return null;  // priority-chain gate
        }
        String strategy = extractTopLevelString(obj, "strategy");
        Double confidence = extractNumberField(obj, "confidence");
        String reason = extractTopLevelString(obj, "reason");
        return new Candidate(selector, strategy, confidence, reason);
    }

    private static Double extractNumberField(String obj, String key) {
        Pattern p = Pattern.compile(
            "\"" + Pattern.quote(key) + "\"\\s*:\\s*(null|(-?\\d+(?:\\.\\d+)?))"
        );
        java.util.regex.Matcher m = p.matcher(obj);
        if (m.find()) {
            if ("null".equals(m.group(1))) return null;
            try { return Double.parseDouble(m.group(2)); }
            catch (NumberFormatException e) { return null; }
        }
        return null;
    }

    /** Find the matching close bracket/brace, honouring JSON string escapes
     *  so nested {@code "} characters don't confuse depth tracking. */
    private static int findBalancedJson(String src, int start, char open, char close) {
        int depth = 0;
        boolean inString = false;
        boolean escape = false;
        for (int i = start; i < src.length(); i++) {
            char c = src.charAt(i);
            if (escape) { escape = false; continue; }
            if (inString) {
                if (c == '\\') escape = true;
                else if (c == '"') inString = false;
                continue;
            }
            if (c == '"') { inString = true; continue; }
            if (c == open) depth++;
            else if (c == close) {
                depth--;
                if (depth == 0) return i;
            }
        }
        return -1;
    }

    // ----------------------------------------------------------------------
    // JSON helpers (minimal, hand-rolled)
    // ----------------------------------------------------------------------

    private static String jsonString(String s) {
        if (s == null) return "null";
        return "\"" + escapeJson(s) + "\"";
    }

    private static String escapeJson(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        return sb.toString();
    }

    private static String unescapeJson(String s) {
        StringBuilder sb = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\' && i + 1 < s.length()) {
                char n = s.charAt(++i);
                switch (n) {
                    case '"': sb.append('"'); break;
                    case '\\': sb.append('\\'); break;
                    case '/': sb.append('/'); break;
                    case 'n': sb.append('\n'); break;
                    case 'r': sb.append('\r'); break;
                    case 't': sb.append('\t'); break;
                    case 'u':
                        if (i + 4 < s.length()) {
                            sb.append((char) Integer.parseInt(s.substring(i + 1, i + 5), 16));
                            i += 4;
                        }
                        break;
                    default: sb.append(n);
                }
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    // ----------------------------------------------------------------------
    // Logger
    // ----------------------------------------------------------------------

    private static void log(String event, String... kvPairs) {
        StringBuilder sb = new StringBuilder("qtea {\"event\":\"").append(event).append("\"");
        for (int i = 0; i + 1 < kvPairs.length; i += 2) {
            sb.append(",\"").append(kvPairs[i]).append("\":")
              .append(jsonString(kvPairs[i + 1]));
        }
        sb.append("}");
        System.err.println(sb);
    }
}
