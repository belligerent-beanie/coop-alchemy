// The site under test and its pages. SITE defaults to a local server. Set ALCHEMY_USER and ALCHEMY_PASSWORD to have axe-core
// sign in through the real login form and check the signed-in pages too (Lighthouse resets storage, so it checks public pages only).
export const SITE = (process.env.SITE ?? "http://127.0.0.1:8000").replace(/\/$/, "");
export const USER = process.env.ALCHEMY_USER ?? "";
export const PASSWORD = process.env.ALCHEMY_PASSWORD ?? "";
export const PUBLIC_PAGES = ["/login", "/terms", "/privacy"];
export const SIGNED_IN_PAGES = ["/", "/guides", "/icons", "/icons/set"];
