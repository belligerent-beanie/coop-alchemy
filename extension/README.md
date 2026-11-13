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
a page that is nothing like the game's: a strict content security policy, generated class names, its own CSS variables. 25 checks: the UI in
a shadow root and styled despite the CSP; selecting without the click reaching the page; the page's own colour variables in the palette;
edits through the CSSOM; typing and focus inside the shadow root; storage out of the page's reach; edits coming back; the toolbar toggle;
the export; a preview window of the right size; and that the shipping manifest asks for nothing broad. `test_extension.py` in the main
suite guards the build (the picker copied byte for byte, the manifest, the files it names).

## Why not fork VisBug?

Google's VisBug does much of this on any page and is Apache-2.0. Its repository was **archived by its owner on 15 September 2026**
(read-only, 213 open issues) and a Web Store reviewer says it has no undo; it has no contrast check or Markdown hand-off that I could find.
Forking would mean owning an unmaintained Rollup/Firebase codebase and then adding what ours already has. Sources:
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
