package com.worca.runtime;

import com.microsoft.playwright.Frame;
import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.Set;

/**
 * Entry point for the worca-t JIT locator runtime (Java + Playwright).
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
 *     this.page = WorcaT.wrap(raw);
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
public final class WorcaT {

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

    private WorcaT() {}

    /**
     * Wrap a {@link Page} in a dynamic proxy that intercepts sentinel
     * locator calls. Returns the input unchanged when the runtime is
     * disabled via {@code WORCA_T_DISABLE_JIT=1}.
     *
     * @param raw the real Page returned by {@code BrowserContext.newPage()}.
     * @return a proxy Page, or {@code raw} when JIT is disabled.
     */
    public static Page wrap(Page raw) {
        if ("1".equals(System.getenv("WORCA_T_DISABLE_JIT"))) {
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
        if ("0".equals(System.getenv("WORCA_T_INFLATE_TIMEOUTS"))) return;
        try {
            String envMs = System.getenv("WORCA_T_DEFAULT_TIMEOUT_MS");
            double ms = (envMs != null) ? Double.parseDouble(envMs) : 60_000d;
            page.setDefaultTimeout(ms);
        } catch (Throwable t) {
            // logging must never throw
        }
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
        protected WorcaTResolver.Resolution resolution;
        protected boolean retried = false;

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
            resolution = WorcaTResolver.resolveSentinel(page, sentinel, false, false, false);
            if (resolution.selector == null) {
                throw new RuntimeException(
                    "worca-t JIT runtime: could not resolve locator '"
                    + Tbd.parseSentinel(sentinel) + "'. See stderr for diagnostic."
                );
            }
            real = buildReal(resolution.selector);
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
            // Retriable action: try once, on timeout invalidate + re-resolve + retry once.
            try {
                return method.invoke(real, args);
            } catch (InvocationTargetException invocation) {
                Throwable cause = invocation.getCause();
                if (!WorcaTResolver.isPlaywrightTimeout(cause)) throw cause;
                retried = true;
                // Invalidate the cache entry for stale dev/cache/agent sources.
                if ("cached".equals(resolution.source) || "agent".equals(resolution.source)) {
                    WorcaTResolver.invalidateCacheEntry(
                        resolution.testFile, resolution.constantName, resolution.intent
                    );
                }
                WorcaTResolver.Resolution fresh = WorcaTResolver.resolveSentinel(
                    page, sentinel,
                    "dev".equals(resolution.source),
                    true,                                  // skip cache (we just invalidated)
                    "heuristic".equals(resolution.source) // skip heuristic if it just failed
                );
                if (fresh.selector == null) {
                    throw cause;  // propagate original timeout
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
