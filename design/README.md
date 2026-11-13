# Design workspace

UI/UX tooling for the game's designers. It is separate from the game (a Python server with plain static files): nothing here is served,
built into the Docker image (`design` is in `.dockerignore`) or needed to run or deploy the game. Use `bun`, not npm.

    cd design && bun install

| Tool | Command | What it is for |
|---|---|---|
| Storybook | `bun run storybook` (port 6006) | Build and try components in isolation. `bun run build-storybook` makes a static copy. |
| shadcn/ui + Tailwind 4 | `bunx --bun shadcn@latest add <component>` | Components land in `src/components/ui`. Config: `components.json`. |
| Chromatic | `bun run chromatic` | Publishes Storybook and flags **visual changes** to components for review. Needs a project token (below). |
| axe-core | `bun run a11y:site` | WCAG 2.0/2.1/2.2 A and AA checks on the real pages of the running game. The Storybook *Accessibility* panel runs the same engine on components. |
| Lighthouse | `bun run lighthouse` | Performance, accessibility, best practices and SEO of the public pages; reports in `reports/` (not in git). |

`a11y:site` and `lighthouse` aim at `SITE` (default `http://127.0.0.1:8000`). For `a11y:site` to include the signed-in pages, also set
`ALCHEMY_USER` and `ALCHEMY_PASSWORD`; it signs in through the login form. Point them at a **test** server, not live. Both exit
non-zero or print what failed, so they can be put in a CI step later.

## Chromatic token (a human step)

1. Sign in at <https://www.chromatic.com> with GitHub and add this repository as a project.
2. Copy its project token and put it in your shell: `CHROMATIC_PROJECT_TOKEN=...` (never commit it).
3. `bun run chromatic`. The first run sets the baselines; later runs show what changed and wait for your accept or deny.

Storybook is also set to fail on accessibility violations only in its test UI for now (`a11y.test: 'todo'` in
`.storybook/preview.tsx`); change it to `'error'` once the components are clean.
