package com.worca.runtime;

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
final class WorcaTResolver {

    static final class Resolution {
        final String selector;
        final String source;          // "dev" | "cached" | "heuristic" | "agent" | "none"
        final String constantName;
        final String intent;
        final String testFile;

        Resolution(String selector, String source, String constantName,
                   String intent, String testFile) {
            this.selector = selector;
            this.source = source;
            this.constantName = constantName;
            this.intent = intent;
            this.testFile = testFile;
        }
    }

    private WorcaTResolver() {}

    // ----------------------------------------------------------------------
    // Public entry point
    // ----------------------------------------------------------------------

    static Resolution resolveSentinel(
        Page page, String sentinel,
        boolean skipDev, boolean skipCache, boolean skipHeuristic
    ) {
        String intent = Tbd.parseSentinel(sentinel);
        String constantName = intent.substring(0, Math.min(intent.length(), 64));
        String testFile = System.getProperty("worca_t.current_test_file");
        if (testFile == null) testFile = System.getenv("WORCA_T_CURRENT_TEST_FILE");

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
                return new Resolution(cached, "cached", constantName, intent, testFile);
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
        if ("1".equals(System.getenv("WORCA_T_NO_LLM_RESOLVE"))) {
            log("no_llm_resolve_active", "constant", constantName, "intent", intent);
            return new Resolution(null, "none", constantName, intent, testFile);
        }
        String llmSelector = callResolverServer(intent, constantName, snapshotText, testFile, safePageUrl(page));
        if (llmSelector == null) {
            log("resolver_failed", "constant", constantName, "intent", intent);
            return new Resolution(null, "none", constantName, intent, testFile);
        }
        log("resolver_ok", "constant", constantName, "selector", llmSelector);
        return new Resolution(llmSelector, "agent", constantName, intent, testFile);
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
    // Snapshot
    // ----------------------------------------------------------------------

    private static String snapshotPage(Page page) {
        // Playwright-Java exposes accessibility via `page.accessibility().snapshot()`
        // but the signature varies by version. Use reflection to stay loose.
        try {
            Object accessibility = Page.class.getMethod("accessibility").invoke(page);
            Object snapshot = accessibility.getClass().getMethod("snapshot").invoke(accessibility);
            if (snapshot == null) return "{}";
            return jsonStringifyAom(snapshot);
        } catch (Throwable t) {
            log("snapshot_failed", "error", t.toString());
            return "{}";
        }
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
            synchronized (WorcaTResolver.class) {
                if (devLocatorsCache == null) devLocatorsCache = loadDevLocators();
            }
        }
        return devLocatorsCache.get(constantName);
    }

    private static Map<String, String> loadDevLocators() {
        List<Path> candidates = new ArrayList<>();
        String env = System.getenv("WORCA_T_DEV_LOCATORS");
        if (env != null) candidates.add(Paths.get(env));
        candidates.add(Paths.get("").toAbsolutePath().resolve(".worca-t/dev-locators.json"));
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
        String dir = System.getenv("WORCA_T_CACHE_DIR");
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

    private static String callResolverServer(
        String intent, String constantName, String snapshotText,
        String testFile, String pageUrl
    ) {
        String portStr = System.getenv("WORCA_T_RESOLVER_PORT");
        String token = System.getenv("WORCA_T_RESOLVER_TOKEN");
        if (portStr == null || token == null) {
            log("resolver_socket_no_env", "hint",
                "WORCA_T_RESOLVER_PORT/TOKEN not set; resolver disabled");
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
            return extractSelectorFromJson(line);
        } catch (IOException e) {
            log("resolver_socket_error", "error", e.toString());
            return null;
        }
    }

    private static String extractSelectorFromJson(String responseLine) {
        if (responseLine.contains("\"ok\":false")) return null;
        Pattern p = Pattern.compile("\"selector\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");
        java.util.regex.Matcher m = p.matcher(responseLine);
        if (m.find()) {
            String sel = unescapeJson(m.group(1));
            return sel.isEmpty() ? null : sel;
        }
        return null;
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
        StringBuilder sb = new StringBuilder("worca_t {\"event\":\"").append(event).append("\"");
        for (int i = 0; i + 1 < kvPairs.length; i += 2) {
            sb.append(",\"").append(kvPairs[i]).append("\":")
              .append(jsonString(kvPairs[i + 1]));
        }
        sb.append("}");
        System.err.println(sb);
    }
}
