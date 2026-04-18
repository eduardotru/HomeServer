// Single source of truth for the top navigation across all chat static pages.
//
// Usage: in any page, put an empty `<nav data-shared-nav></nav>` element in
// the header and include this file as a module:
//
//     <script type="module" src="/static/js/nav.js"></script>
//
// The module renders the nav automatically on DOMContentLoaded (or immediately
// if the DOM is already parsed). It also exports `NAV_ITEMS` and
// `renderNav()` for callers that want to invoke it explicitly.

export const NAV_ITEMS = [
    { href: "/", label: "chat", match: (p) => p === "/" || p.startsWith("/chat") },
    { href: "/search", label: "search", match: (p) => p === "/search" },
    { href: "/notes", label: "notes", match: (p) => p === "/notes" },
    { href: "/routines", label: "routines", match: (p) => p === "/routines" },
    // "apps" is active on /apps and on any /apps/<name> wrapper URL.
    { href: "/apps", label: "apps", match: (p) => p === "/apps" || p.startsWith("/apps/") },
];

export function renderNav(root = document) {
    const nav = root.querySelector("nav[data-shared-nav]");
    if (!nav) return;

    const pathname = window.location.pathname;
    // Clear anything that was there (e.g. a noscript fallback).
    nav.replaceChildren();

    for (const item of NAV_ITEMS) {
        const a = document.createElement("a");
        a.className = "nav-link" + (item.match(pathname) ? " active" : "");
        a.href = item.href;
        a.textContent = item.label;
        nav.appendChild(a);
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => renderNav());
} else {
    renderNav();
}
