package com.qtea.runtime;

import com.microsoft.playwright.Frame;
import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;

import java.io.IOException;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * Entry point for the qtea JIT locator runtime (Java + Playwright).
 *
 * <p>Java's classloader does not permit runtime method replacement the way
 * Python and JavaScript do, so the wrap must be explicit: every test
 * fixture / hook that returns a {@link Page} calls {@link #wrap(Page)}
 * once before handing it to test code. The returned {@link Page} is a
 * JDK dynamic proxy that intercepts {@code locator(String)} calls and
 * resolves sentinels produced by {@link Tbd#of(String)} against the live
 * page. Resolved locators are likewise wrapped in a {@link Locator}
 * proxy that retries an action once on {@code TimeoutError} after
 * cache-invalidate + re-resolve.
 *
 * <p>Usage:
 * <pre>
 * &#64;BeforeEach
 * void setUp() {
 *     Page raw = browserContext.newPage();
 *     this.page = QteaT.wrap(raw);
 * }
 * </pre>
 *
 * <p><b>Java constraint:</b> declare locals as {@link Page} / {@link Locator}
 * (the interfaces), never the impl classes — the proxy only works through
 * interface types. The codegen agent enforces this; the indexer flags
 * casts to impl classes as a violation.
 *
 * <p>This file is best-effort vendored — when Playwright-Java is on the
 * classpath the wrap activates; when absent, {@link #wrap(Page)} returns
 * the input unchanged so test code compiles cleanly in either case.
 */
public final class QteaT {

    /** Action methods that can raise {@code PlaywrightException} with a
     *  timeout message. Mirrors {@code _RETRIABLE_METHODS} in the Python
     *  and TypeScript runtimes. */
    private static final Set<String> RETRIABLE_METHODS = Set.of(
        "click", "dblclick", "tap", "hover",
        "fill", "press", "pressSequentially", "type",
        "check", "uncheck", "setChecked", "setInputFiles",
        "selectOption", "selectText",
        "dragTo", "screenshot", "focus", "blur",
        "scrollIntoViewIfNeeded", "clear", "dispatchEvent",
        "waitFor", "textContent", "innerText", "innerHTML",
        "inputValue", "getAttribute", "evaluate", "evaluateHandle",
        "isVisible", "isHidden", "isEnabled", "isDisabled",
        "isChecked", "isEditable"
    );

    private QteaT() {}

    /**
     * Wrap a {@link Page} in a dynamic proxy that intercepts sentinel
     * locator calls. Returns the input unchanged when the runtime is
     * disabled via {@code QTEA_DISABLE_JIT=1}.
     *
     * @param raw the real Page returned by {@code BrowserContext.newPage()}.
     * @return a proxy Page, or {@code raw} when JIT is disabled.
     */
    public static Page wrap(Page raw) {
        if ("1".equals(System.getenv("QTEA_DISABLE_JIT"))) {
            return raw;
        }
        if (raw == null) return null;
        inflateTimeouts(raw);
        return (Page) Proxy.newProxyInstance(
            Page.class.getClassLoader(),
            new Class<?>[]{Page.class},
            new PageHandler(raw)
        );
    }

    private static void inflateTimeouts(Page page) {
        if ("0".equals(System.getenv("QTEA_INFLATE_TIMEOUTS"))) return;
        try {
            String envMs = System.getenv("QTEA_DEFAULT_TIMEOUT_MS");
            double ms = (envMs != null) ? Double.parseDouble(envMs) : 60_000d;
            page.setDefaultTimeout(ms);
        } catch (Throwable t) {
            // logging must never throw
        }
    }

    // ----------------------------------------------------------------------
    // On-failure AOM capture — writes
    // <QTEA_WORKSPACE_DIR>/aom-at-failure/<entry_id>.txt so Step 9 Layer 2
    // can cross-check the failure against the live page AOM.
    //
    // Wire from @AfterEach (JUnit 5) / @AfterMethod (TestNG):
    //   QteaT.captureAomOnFailure(page, "ClassName", "methodName");
    // Best-effort — capture failures never propagate.
    // ----------------------------------------------------------------------

    /**
     * Match Python {@code md_parser.slugify} — ascii-alnum + hyphens,
     * lowercased. Public so operators can build their own IDs.
     */
    public static String slugify(String s) {
        if (s == null) return "untitled";
        String base = s.replaceAll("[^A-Za-z0-9]+", "-");
        base = base.replaceAll("^-+", "").replaceAll("-+$", "").toLowerCase();
        return base.isEmpty() ? "untitled" : base;
    }

    /**
     * Match Python {@code test_runner._normalize_id(file_rel, name)}. When
     * {@code fileRel} is non-null, the file's stem is prefixed to
     * {@code name}; otherwise {@code name} is slugified directly. Result
     * format: {@code T-<slug>}. Same output shape as the Python and JS
     * runtimes, so the parent qtea process reads the same entry_id from
     * JUnit XML and from our on-failure capture.
     */
    public static String normalizeTestId(String fileRel, String name) {
        String combined;
        if (fileRel != null && !fileRel.isEmpty()) {
            Path p = Paths.get(fileRel);
            String stem = p.getFileName() != null ? p.getFileName().toString() : "";
            int dot = stem.lastIndexOf('.');
            if (dot > 0) stem = stem.substring(0, dot);
            combined = stem + "-" + (name == null ? "" : name);
        } else {
            combined = name == null ? "" : name;
        }
        return "T-" + slugify(combined);
    }

    /**
     * Snapshot the page AOM and write it to
     * {@code <QTEA_WORKSPACE_DIR>/aom-at-failure/<entryId>.txt}. Silently
     * no-ops when the workspace env-var is unset or the page is unusable —
     * capture failures must never affect the test outcome.
     *
     * @param page       the wrapped or raw Page instance
     * @param entryId    a {@code T-<slug>} identifier; use
     *                   {@link #normalizeTestId(String, String)} to build one
     */
    public static void captureAomOnFailure(Page page, String entryId) {
        try {
            String workspace = System.getenv("QTEA_WORKSPACE_DIR");
            if (workspace == null || workspace.isEmpty()) return;
            if (page == null) return;
            if (entryId == null || entryId.isEmpty()) return;
            String snap = QteaTResolver.snapshotPageForCapture(page);
            if (snap == null || snap.isEmpty()) return;
            Path outDir = Paths.get(workspace, "aom-at-failure");
            Files.createDirectories(outDir);
            Path outFile = outDir.resolve(entryId + ".txt");
            Files.write(outFile, snap.getBytes(StandardCharsets.UTF_8));
        } catch (Throwable t) {
            try { System.err.println("qtea aom_capture_on_failure_failed: " + t); }
            catch (Throwable ignored) {}
        }
    }

    /**
     * Convenience overload — builds the entry_id from class + method name
     * using the same format the parent qtea process uses when parsing
     * JUnit XML output. Passing {@code null} class or method falls back to
     * {@code "unknown"} for that segment.
     *
     * <p>Note: the parent process's {@code _normalize_id(file_rel, name)}
     * uses the source-file's stem, not the class name. For JUnit, class
     * name and source-file stem usually match (Java convention). For
     * TestNG with multiple test classes per file, the operator may need to
     * build the entry_id manually via {@link #normalizeTestId} to match
     * the parent's expected file_rel.
     */
    public static void captureAomOnFailure(Page page, String className, String methodName) {
        String cls = className == null ? "unknown" : className;
        String mth = methodName == null ? "unknown" : methodName;
        captureAomOnFailure(page, normalizeTestId(cls, mth));
    }

    // ----------------------------------------------------------------------
    // Page proxy
    // ----------------------------------------------------------------------

    private static final class PageHandler implements InvocationHandler {
        private final Page real;
        PageHandler(Page real) { this.real = real; }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            if ("locator".equals(method.getName()) && args != null && args.length >= 1
                    && args[0] instanceof String) {
                String selector = (String) args[0];
                if (Tbd.isSentinel(selector)) {
                    return interceptLocator(real, selector, args);
                }
                // Non-sentinel locator: wrap the returned Locator in a
                // sub-locator-aware proxy so that any nested .locator(SENTINEL)
                // call is still intercepted. The overhead is one extra method
                // dispatch per call — negligible compared to a real Playwright
                // selector resolution.
                Object child;
                try { child = method.invoke(real, args); }
                catch (InvocationTargetException e) { throw e.getCause(); }
                if (child instanceof Locator) {
                    return wrapChildLocator(real, (Locator) child);
                }
                return child;
            }
            try {
                return method.invoke(real, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        }
    }

    /**
     * Wrap a sentinel-free child Locator (e.g. result of
     * {@code page.locator("main")}) so its own {@code .locator(...)} calls
     * still detect sentinels. The wrapped Locator forwards everything
     * else verbatim.
     */
    private static Locator wrapChildLocator(Page page, Locator real) {
        return (Locator) Proxy.newProxyInstance(
            Locator.class.getClassLoader(),
            new Class<?>[]{Locator.class},
            new SubLocatorHandler(page, real)
        );
    }

    private static final class SubLocatorHandler implements InvocationHandler {
        private final Page page;
        private final Locator real;
        SubLocatorHandler(Page page, Locator real) { this.page = page; this.real = real; }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            if ("locator".equals(method.getName()) && args != null && args.length >= 1
                    && args[0] instanceof String) {
                String selector = (String) args[0];
                if (Tbd.isSentinel(selector)) {
                    // Sub-locator sentinel: build a LazyResolvingLocator that
                    // composes with the real parent (this.real.locator(...)).
                    return (Locator) Proxy.newProxyInstance(
                        Locator.class.getClassLoader(),
                        new Class<?>[]{Locator.class},
                        new LazyResolvingLocator(page, selector, args) {
                            // Override: rebuild against the parent real Locator,
                            // not the Page, so the sub-locator scope is preserved.
                            @Override
                            protected Locator buildReal(String resolvedSelector) {
                                return real.locator(resolvedSelector);
                            }
                        }
                    );
                }
                Object child;
                try { child = method.invoke(real, args); }
                catch (InvocationTargetException e) { throw e.getCause(); }
                if (child instanceof Locator) {
                    return wrapChildLocator(page, (Locator) child);
                }
                return child;
            }
            try {
                return method.invoke(real, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        }
    }

    /** Build a Locator for a sentinel call. Resolves lazily — the
     *  resolution happens at the first ACTION method call on the returned
     *  Locator, mirroring the JS runtime's lazy strategy. */
    private static Locator interceptLocator(Page page, String sentinel, Object[] originalArgs) {
        return (Locator) Proxy.newProxyInstance(
            Locator.class.getClassLoader(),
            new Class<?>[]{Locator.class},
            new LazyResolvingLocator(page, sentinel, originalArgs)
        );
    }

    // ----------------------------------------------------------------------
    // Locator proxy with lazy resolution + retry-on-timeout
    // ----------------------------------------------------------------------

    /** Visible to package so {@code SubLocatorHandler} can subclass and
     *  override {@link #buildReal(String)} to scope the resolved locator
     *  to a parent Locator instead of the Page. */
    static class LazyResolvingLocator implements InvocationHandler {
        protected final Page page;
        protected final String sentinel;
        protected final Object[] originalLocatorArgs;
        protected Locator real;
        protected QteaTResolver.Resolution resolution;
        protected boolean retried = false;  // guards the LLM re-resolve to one attempt
        /** Fallback candidates from the bundle (everything past candidates[0],
         *  which is already wrapped in {@code real}). Walked on TimeoutError
         *  BEFORE invalidate + re-resolve, so a one-mutation fallback costs
         *  zero LLM tokens. Empty when resolution carries no bundle. */
        protected List<QteaTResolver.Candidate> remainingCandidates;
        protected int totalFallbacks;

        LazyResolvingLocator(Page page, String sentinel, Object[] originalArgs) {
            this.page = page;
            this.sentinel = sentinel;
            this.originalLocatorArgs = originalArgs;
        }

        /** Construct a real Locator from the resolved selector. Default
         *  scope: the Page. Subclass overrides this to scope to a parent
         *  Locator (sub-locator chaining). */
        protected Locator buildReal(String resolvedSelector) {
            return page.locator(resolvedSelector);
        }

        private synchronized void ensureResolved() {
            if (real != null) return;
            resolution = QteaTResolver.resolveSentinel(page, sentinel, false, false, false);
            if (resolution.selector == null) {
                throw new RuntimeException(
                    "qtea JIT runtime: could not resolve locator '"
                    + Tbd.parseSentinel(sentinel) + "'. See stderr for diagnostic."
                );
            }
            real = buildReal(resolution.selector);
            if (resolution.candidates != null && resolution.candidates.size() > 1) {
                remainingCandidates = new ArrayList<>(
                    resolution.candidates.subList(1, resolution.candidates.size())
                );
                totalFallbacks = remainingCandidates.size();
            } else {
                remainingCandidates = new ArrayList<>();
                totalFallbacks = 0;
            }
        }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
            ensureResolved();
            // Non-retriable methods: pass straight through.
            if (!RETRIABLE_METHODS.contains(method.getName()) || retried) {
                try {
                    return method.invoke(real, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            }
            // Retriable action: walk in-bundle fallbacks first, then fall
            // back to invalidate + LLM re-resolve.
            while (true) {
                try {
                    Object result = method.invoke(real, args);
                    // Success — promote the working fallback (if one was used).
                    if (remainingCandidates.size() < totalFallbacks) {
                        int usedIdx = totalFallbacks - remainingCandidates.size();  // 1..N
                        QteaTResolver.Candidate working = resolution.candidates.get(usedIdx);
                        QteaTResolver.promoteCandidateInCache(
                            resolution.testFile, resolution.constantName,
                            resolution.intent, working
                        );
                    }
                    return result;
                } catch (InvocationTargetException invocation) {
                    Throwable cause = invocation.getCause();
                    if (!QteaTResolver.isPlaywrightTimeout(cause)) throw cause;
                    if (!remainingCandidates.isEmpty()) {
                        QteaTResolver.Candidate nxt = remainingCandidates.remove(0);
                        if (nxt != null && nxt.selector != null && !nxt.selector.isEmpty()) {
                            real = buildReal(nxt.selector);
                            continue;  // retry against the fallback candidate
                        }
                    }
                    // Bundle exhausted (or never existed) → LLM re-resolve.
                    retried = true;
                    if ("cached".equals(resolution.source) || "agent".equals(resolution.source)) {
                        QteaTResolver.invalidateCacheEntry(
                            resolution.testFile, resolution.constantName, resolution.intent
                        );
                    }
                    QteaTResolver.Resolution fresh = QteaTResolver.resolveSentinel(
                        page, sentinel,
                        "dev".equals(resolution.source),
                        true,
                        "heuristic".equals(resolution.source)
                    );
                    if (fresh.selector == null) {
                        throw cause;
                    }
                    real = buildReal(fresh.selector);
                    resolution = fresh;
                    try {
                        return method.invoke(real, args);
                    } catch (InvocationTargetException e) {
                        throw e.getCause();
                    }
                }
            }
        }
    }
}
