const ALL_SITES = { origins: ["<all_urls>"] };
const state = document.getElementById("state");
async function show() { state.textContent = (await chrome.permissions.contains(ALL_SITES)) ? "allowed" : "not allowed"; }
document.getElementById("allow").addEventListener("click", async () => { await chrome.permissions.request(ALL_SITES); await show(); });
document.getElementById("revoke").addEventListener("click", async () => { await chrome.permissions.remove(ALL_SITES); await show(); });
show().catch(console.error);
