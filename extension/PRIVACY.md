# Design Picker: privacy policy

*Last updated 13 November 2026.*

**Design Picker collects no data.** Nothing about you, your browsing or the pages you work on is sent anywhere, by the extension or by anyone
on its behalf. It has no server, no account, no analytics and no tracking.

## What it stores, and where

When you restyle something on a page, the extension keeps **your edits** (which element, and the before and after values of the properties you
changed) so they can be put back after a reload. They are stored in your browser with `chrome.storage.local`, under the address of the page.
They never leave your browser, and the page's own scripts cannot read them. Remove them by resetting the changes in the picker, or by removing
the extension. It also remembers that you have seen its first-time guide.

## What it does with the pages you use it on

It runs only on a tab where you turn it on (the toolbar button or Alt+Shift+P), unless you choose to allow all sites. It reads the page's styles
and structure so it can show you values and measure distances, and it changes what the page looks like **in your browser only**: the page's
server and other visitors never see your edits. The text it copies to your clipboard (when you press Copy) is made on your device and goes only
where you paste it.

## Permissions, and why

| Permission | Why |
|---|---|
| `activeTab` | To run on the tab you clicked the extension on, and nothing else, until you leave that page. |
| `scripting` | To put the picker on that tab, and (with all sites allowed) to restore your saved edits after a reload and to put them on a preview window. |
| `storage` | To keep your edits and the guide flag in your browser, as above. |
| *All sites* (optional, asked only if you press **Allow all sites** in the options) | To restore your saved edits automatically after a reload (a small script checks whether the page has saved edits, and does nothing if it has none) and to put your edits on a screen-size preview window. You can remove it again in the options. |

## Changes and contact

If this policy changes, the new version replaces this file in the repository with a new date. Questions or concerns: open an issue at
<https://github.com/belligerent-beanie/coop-alchemy/issues>. The extension is open source: <https://github.com/belligerent-beanie/coop-alchemy/tree/main/extension>.
