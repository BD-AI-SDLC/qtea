package com.qtea.runtime;

/**
 * Sentinel helper for the qtea JIT locator runtime (Java + Playwright).
 *
 * <p>Vendored into the SUT at Step 7 codegen time. Codegen emits unresolved
 * locator constants as {@code Tbd.of("intent")} returning a sentinel
 * string that {@link QteaT}'s page proxy intercepts at runtime.
 *
 * <p>Mirrors the Python and TypeScript sentinel layout exactly:
 * {@code __QTEA_TBD__::<intent>}. A single resolver subprocess /
 * ResolverServer can serve all three runtimes because they share this
 * on-the-wire format.
 *
 * <p>Usage in Page Object Model classes:
 * <pre>
 * import com.qtea.runtime.Tbd;
 *
 * public final class LoginLocators {
 *     public static final String LOGIN_BUTTON   = Tbd.of("primary submit button on the login form");
 *     public static final String PASSWORD_INPUT = Tbd.of("password input on the sign-in form");
 * }
 * </pre>
 *
 * <p>Test code then references the constants as it normally would:
 * {@code page.locator(LoginLocators.LOGIN_BUTTON).click();} — when
 * {@code page} is a {@link QteaT#wrap(com.microsoft.playwright.Page) wrapped}
 * Page, the sentinel resolves transparently.
 */
public final class Tbd {

    /** Sentinel prefix. Identical to the Python + TypeScript runtimes. */
    public static final String SENTINEL_PREFIX = "__QTEA_TBD__::";

    private Tbd() {}

    /**
     * Mark a locator constant as unresolved.
     *
     * @param intent human-readable description of the element. Must be non-blank.
     * @return a sentinel string that {@link QteaT#wrap(com.microsoft.playwright.Page)}'s
     *         Page proxy will intercept and resolve at runtime.
     * @throws IllegalArgumentException if {@code intent} is null or blank.
     */
    public static String of(String intent) {
        if (intent == null || intent.trim().isEmpty()) {
            throw new IllegalArgumentException("Tbd.of() requires a non-empty intent string");
        }
        return SENTINEL_PREFIX + intent.trim();
    }

    /** @return true when {@code value} is a TBD sentinel produced by {@link #of(String)}. */
    public static boolean isSentinel(String value) {
        return value != null && value.startsWith(SENTINEL_PREFIX);
    }

    /** Extract the intent string from a sentinel. Caller is responsible for
     *  ensuring {@link #isSentinel(String)} returns true first. */
    public static String parseSentinel(String value) {
        return value.substring(SENTINEL_PREFIX.length());
    }
}
