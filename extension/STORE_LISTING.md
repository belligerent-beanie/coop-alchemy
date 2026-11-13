# Chrome Web Store listing (draft)

Everything the developer dashboard asks for, ready to paste. A human step remains: a developer account (a one-time fee), the upload, and the
review. Build the file to upload with `node extension/build.mjs --zip` (it writes `extension/design-picker-<version>.zip`).

**Decision for you before publishing:** the name. "Design Picker" is generic and may collide with another listing; check the store, and rename
in `manifest.template.json` (`name`) if so. Do not use "VisBug" (Apache-2.0 gives no right to the name).

## Basics

| Field | Value |
|---|---|
| Name | Design Picker |
| Category | Developer Tools |
| Language | English |
| Summary (from the manifest, 132 characters at most) | Select, restyle and measure any element on any page, check contrast, and hand the changes to a developer or Claude as Markdown. |
| Privacy policy URL | https://github.com/belligerent-beanie/coop-alchemy/blob/main/extension/PRIVACY.md |
| Homepage / support URL | https://github.com/belligerent-beanie/coop-alchemy/tree/main/extension |
| Screenshots (1280 × 800) | `extension/store/screenshots/*.png` (regenerate with `node extension/scripts/screenshots.mjs`) |
| Small promo tile (440 × 280) | not made yet: required by the store |

## Detailed description

> Design Picker lets you try design changes on a live page and hand them over in a form a developer (or Claude) can apply.
>
> **Select and change** any element: fill, border, shadow, text, font (colour, size, weight, line height, spacing, alignment), size (type a
> number or drag the handles), padding and margin, corner radius, and flex and grid layout. Shift-click to select several elements and change
> them all at once. Copy one element's look and paste it onto others.
>
> **Check as you go.** Every colour picker shows the WCAG contrast ratio (AA and AAA) and the APCA contrast value live, and lists the colours
> the page already uses, with the names of its CSS variables. Hover anything with the inspector to see its size, type, colours, contrast and
> the name a screen reader would announce. Hold a key to measure distances between elements. Hold another to flip back to the original page.
>
> **Work safely.** Undo and redo every step. Edits are kept per page and come back after a reload. Preview your edits at phone, tablet and
> laptop sizes in a real browser window.
>
> **Hand it off.** Copy every change as Markdown (the selector, then before and after for each property, and what the change did to the
> contrast), ready to paste to a developer or into a chat with Claude.
>
> Private by design: no account, no servers, no analytics. Your edits stay in your browser. Open source.

## Single purpose

Lets a designer select, restyle and measure elements on a web page to try changes and export them as a change list.

## Permission justifications (for the review form)

- **activeTab**: runs the picker only on the tab the user clicks the extension on; no other tab is touched.
- **scripting**: injects the picker into that tab; when the user has allowed all sites, also restores their saved edits after a reload and puts them on a preview window.
- **storage**: keeps the user's edits for each page, and a flag that the first-time guide has been seen, in the browser.
- **Optional host permission (all sites)**: requested only when the user presses *Allow all sites* on the options page, so that saved edits are restored automatically after a reload and screen-size previews show the edits. A small script checks local storage for saved edits for the page and does nothing if there are none. It can be removed from the options page at any time.
- **Remote code**: none. All code is in the package; nothing is fetched or evaluated at run time.
- **Data use**: none collected, sold, transferred or used for any purpose other than the single purpose above.

## Release checklist

1. `node extension/test/e2e.mjs` passes (31 checks) and `python -m unittest test_extension` passes.
2. Bump `version` in `extension/package.json`; `node extension/build.mjs --zip`.
3. Upload the zip; paste the fields above; add the screenshots and the promo tile; submit.
