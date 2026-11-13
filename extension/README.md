# Design Picker: the Chrome extension

The designers' UI picker (select an element, restyle it, measure, check contrast, undo, preview other screen sizes, copy the changes as
Markdown for a developer or Claude) as an extension that works on **any site**, not only this game.

It is the same picker. `static/designer.js` is the only copy of its code: the build copies it into the extension unchanged, and the game's
own pages load it the same way as before. Everything that depends on *where* it runs goes through a small host adapter at the top of
`designer.js` ("where it runs"); the game's pages use the defaults, and `host.js` here replaces them:

| | The game's pages | The extension |
|---|---|---|
| Saved edits | `localStorage`, per page | `chrome.storage.local`, per site and page (the page's scripts cannot read it) |
| The picker's UI | in the page | in a Shadow DOM: the page's CSS cannot touch it, and its CSS stays out of the page |
| Styles under a strict CSP | n/a (same origin) | edits use the CSSOM and a constructed stylesheet, which a page's content security policy does not block |
| Design patches | the game's server | none (they belong to the game) |
| Screen-size preview | `window.open` | a real window opened by the service worker |
| Turned on by | an account marked as a designer | the toolbar button or Alt+Shift+P |

## Install (unpacked)

    node extension/build.mjs        # makes extension/dist
    # Chrome: chrome://extensions > Developer mode > Load unpacked > extension/dist

Then click the toolbar icon (or press Alt+Shift+P) on any page. The tools, shortcuts and export are the ones described under *Designers* in
the main README.

## Permissions

`activeTab`, `scripting` and `storage`: the picker runs on the tab you click it on, nothing else, and installing it does not ask to read
every site. Screen-size previews open a second window and need the **optional** all-sites permission to put your edits on it; the options
page (right-click the icon > Options) grants or removes it. Edits stay in this browser and nothing is sent anywhere.

## Tests

    node extension/test/e2e.mjs

Loads the built extension into Playwright's Chromium (branded Chrome has dropped the switch for loading unpacked extensions) and drives it on
a page that is nothing like the game's: a strict content security policy, generated class names, its own CSS variables. 31 checks: the UI in
a shadow root and styled despite the CSP; selecting without the click reaching the page; the page's own colour variables in the palette;
edits through the CSSOM; typing and focus inside the shadow root; storage out of the page's reach; edits coming back by themselves after a
reload, with the picker off; a page with no saved edits left alone; the toolbar toggle; multi-select and one undo for all of it; the
inspector; the export; a preview window of the right size; and that the shipping manifest asks for nothing broad. `test_extension.py` in the
main suite guards the build (the picker copied byte for byte, the manifest and its store limits, the files it names, the zip).

## Releasing to the Chrome Web Store

Everything but the account is ready: `STORE_LISTING.md` (the fields, the permission justifications, a checklist), `PRIVACY.md` (the policy the
store asks for a URL to), the screenshots in `store/screenshots/` (made by `node extension/scripts/screenshots.mjs` from the real extension, so
they can be regenerated whenever the UI changes) and `node extension/build.mjs --zip`, which writes `design-picker-<version>.zip`. Still to do by
a person: a developer account, a 440 x 280 promo tile, the upload and the review, and checking the name ("Design Picker") is free.

## Known limits, and what is next

- **Without the all-sites permission, a reload turns the picker off** (the extension cannot act on a page until asked). Turning it back on
  restores the saved edits. With the optional permission (options page) they come back by themselves, and a page without saved edits is left alone.
- **Hover and focus states** are now possible here (an extension can inject a stylesheet through `chrome.scripting.insertCSS`, which a page's CSP
  does not block) but are not built: each state needs its own rule, and undo, compare, reload and export have to learn about states.
- **Still to port from VisBug** (Apache-2.0, with attribution): moving elements in the DOM, an image swap, a position tool, search by selector,
  alignment guides, hue shift and a screenshot. Done so far: multi-select, the hover inspector with APCA, and (already here) undo, saved edits and the export.
- **Cross-origin iframes** are separate documents the picker does not reach; **closed shadow roots** on a page cannot be selected into.
- Chrome only for now (Edge works the same way); Firefox needs its own manifest and has no side panel API.

## VisBug

Google's VisBug does much of this on any page and is Apache-2.0. Its repository was **archived by its owner on 15 September 2026** (last
commit 2 August 2026; read-only, 213 open issues). I read its source (about 9,200 lines in 100 files, Manifest V3, closed Shadow DOM
components, hotkey-driven tools, `ava` 1.4 and `puppeteer` 10 for tests, `postcss` 7). What it has that this picker does not: multi-select
(Shift-click), moving elements in the DOM, an image swap, a position tool, search by selector, guides, hue shift, a hover inspector with
WCAG AA/AAA **and APCA** contrast, a screenshot, and builds for Firefox, Safari and Edge. What it lacks: any **undo**, any **saved edits**
(nothing survives a reload) and any **export of what changed**. Its tools write inline styles directly (44 places) and record nothing, so
building those on top would mean rewriting every tool to go through a change-recording layer. Apache-2.0 lets us copy code if the licence and
copyright notices stay on what we take and changes are marked; the name "VisBug" is not ours to use. The approach proposed (not yet decided) is to keep
this picker (which has that layer) and port VisBug's missing features into it one at a time, with attribution. Source:
<https://github.com/GoogleChromeLabs/ProjectVisBug>.

## Known limits, and what is next

- **A reload turns the picker off** (with only `activeTab` the extension cannot act on a page until asked). Turning it back on restores the
  saved edits. Putting them back automatically needs a small content script registered, behind the optional all-sites permission, for the
  sites that have saved edits.
- **Hover and focus states** are now possible here (an extension can inject a stylesheet through `chrome.scripting.insertCSS`, which a page's CSP
  does not block) but are not built: each state needs its own rule, and undo, compare, reload and export have to learn about states.
- **Cross-origin iframes** are separate documents the picker does not reach; **closed shadow roots** on a page cannot be selected into.
- Chrome only for now (Edge works the same way); Firefox needs its own manifest and has no side panel API. Publishing on the Web Store needs
  a listing, a privacy statement (there is nothing to disclose: no data leaves the browser) and review.
