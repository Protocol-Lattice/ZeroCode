# Zero product website

A standalone promotional website for the Zero terminal coding agent. It serves
static HTML, CSS, and JavaScript through Node’s
built-in HTTP server. It does not connect to providers or expose the coding agent
in the browser. There are no package dependencies, remote fonts, or analytics.

## Run locally

From the repository root, with Node.js 24 or newer:

```sh
npm run dev
```

Open http://localhost:3000. No `npm install` or build step is needed. Node’s watch
mode restarts the server when its code changes; refresh the browser after editing
static assets.

For a regular server process:

```sh
npm start
```

`HOST` defaults to `127.0.0.1`, and `PORT` defaults to `3000`. To serve in a
container or on your network:

```sh
HOST=0.0.0.0 PORT=8080 npm start
```

For production, deploy the repository to a Node.js host and run `npm start` with
the appropriate `HOST` and `PORT`. Terminate HTTPS at the host or reverse proxy.
Clipboard copying requires HTTPS or localhost; elsewhere, the site selects the
command for manual copying. Only explicitly listed public assets are served.

## GitHub Pages

The [Website workflow](../.github/workflows/website.yml) checks JavaScript and runs
the website tests on pull requests. Changes to the website, its package manifest,
or the workflow on `main` also deploy `website/public` to GitHub Pages after the
checks pass. You can run it manually from the Actions tab with `main` selected.

In the repository, select **Settings → Pages → Build and deployment → Source →
GitHub Actions** before the first deployment. Push these files to `main` or merge
them through a pull request. The `github-pages` environment and successful
deployment expose the published URL. No custom token or deployment secret is
required; the workflow uses GitHub's built-in token and OIDC.

Pages serves the static product website. The Node.js server remains available for
local development and Node.js hosting. Relative asset paths support repository
Pages URLs as well as custom domains. Only `website/public` is uploaded; source
code, tests, configuration, and the terminal application are excluded.

## Checks

```sh
npm run check:website
npm run test:website
```

The server tests check public pages, asset content types, HEAD requests, method
restrictions, rejection of requests for source files or traversal paths, and
asset resolution under a GitHub Pages repository subdirectory.

## Editing

- `public/index.html`: product copy, sections, metadata, and links.
- `public/styles.css`: responsive layout and visual design.
- `public/main.js`: platform tabs, mobile navigation, copy buttons.
- `public/assets/openai-wordmark.svg`: official OpenAI wordmark from
  [OpenAI's brand page](https://openai.com/brand/), owned by OpenAI.
- `server.js`: asset allowlist and HTTP server.

The installation section uses the checked-out repository’s clone-and-build
workflow. macOS and Linux share those commands but have different prerequisite
notes. GitHub and documentation links point at the repository’s configured origin.
