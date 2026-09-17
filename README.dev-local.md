# Local dev setup: CLI and Desktop from this checkout

Personal notes for building and running Hermes from `~/Documents/hermes-agent`
alongside the managed install in `~/.hermes`. Not part of upstream docs.

## Layout on this machine

| What | Where |
|---|---|
| Managed install (owns the `hermes` command) | `~/.hermes/hermes-agent` |
| Managed home: config, auth, sessions, skills | `~/.hermes` |
| Private home with its own `.env`, config, auth | `~/.hermes-private` |
| This dev checkout | `~/Documents/hermes-agent` |
| Dev venv for this checkout | `~/Documents/hermes-agent/.venv` |

Keep the managed install. Both installs share nothing but the home directory
you point them at, and `hermes desktop` depends on the managed one.

## CLI

### Managed install (daily use)

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.zshrc
hermes
hermes update        # later
```

### This checkout (development)

Install the checkout into a venv, then run the CLI via `cli.py`:

```bash
cd ~/Documents/hermes-agent
source .venv/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
python cli.py           # runs this tree, not the managed install
```

To run against the private home:

```bash
HERMES_HOME=~/.hermes-private python cli.py
```

## Desktop app

### Managed build (easiest)

```bash
hermes desktop
```

Installs Node workspace deps, builds the unpacked Electron app, and launches
it. Builds from the managed checkout, not this one. Useful flags:

- `--hermes-root ~/Documents/hermes-agent` runs this tree's Python backend.
- `--source` runs Electron against the Vite build instead of the packaged app.
- `--skip-build` relaunches the last build. `--force-build` ignores the stamp.
- `--setup-tcc-identity` (macOS) creates a local signing cert so permission
  grants survive rebuilds.

### Dev loop from this checkout (hot reload)

One-time: install workspace deps from the repo root. Node 22.22+, 24.11+, or
26+ is required.

```bash
cd ~/Documents/hermes-agent
npm ci
```

Then run the renderer and Electron together:

```bash
cd ~/Documents/hermes-agent/apps/desktop
HERMES_DESKTOP_HERMES_ROOT=~/Documents/hermes-agent npm run dev
```

Renderer edits reload live. Electron main-process edits need a restart.

### Packaged build from this checkout

Same as `hermes desktop`, but with this tree as the project root:

```bash
cd ~/Documents/hermes-agent
source .venv/bin/activate
python cli.py desktop
python cli.py desktop --source        # Electron against Vite output, no pack
python cli.py desktop --skip-build    # relaunch last build
python cli.py desktop --force-build   # rebuild regardless of stamp
```

Artifacts only, no launcher:

```bash
cd apps/desktop
npm run pack          # unpacked app under apps/desktop/release
npm run dist:mac      # DMG + zip
npm run dist:win      # NSIS + MSI
npm run dist:linux    # AppImage + deb + rpm
```

macOS signing and notarization only happen when `CSC_LINK`,
`CSC_KEY_PASSWORD`, and `APPLE_*` are set. Without them you get an ad-hoc
signed build that still runs locally.

## Using the private home (`~/.hermes-private`)

The desktop app reads `HERMES_HOME` and forwards it to the Python backend it
spawns, so the backend loads that directory's `.env`, config, auth, and
sessions.

Dev loop:

```bash
cd ~/Documents/hermes-agent/apps/desktop
HERMES_HOME=~/.hermes-private \
HERMES_DESKTOP_HERMES_ROOT=~/Documents/hermes-agent \
npm run dev
```

Packaged launcher:

```bash
HERMES_HOME=~/.hermes-private \
python cli.py desktop --hermes-root ~/Documents/hermes-agent
```

Why the root override is required: with `HERMES_HOME` set, the app expects a
canonical install at `~/.hermes-private/hermes-agent`. That does not exist, so
without the override it falls back to whatever `hermes` is on PATH, which is
the managed install in `~/.hermes`.

### Running two desktops at once

The app enforces a single instance per app name. If the regular Hermes Desktop
is open, give the private one its own name and user-data directory:

```bash
HERMES_HOME=~/.hermes-private \
HERMES_DESKTOP_HERMES_ROOT=~/Documents/hermes-agent \
HERMES_DESKTOP_APP_NAME=hermes-private \
HERMES_DESKTOP_USER_DATA_DIR=~/.hermes-private/desktop-user-data \
npm run dev
```

Do not combine this with `scripts/dev-sandbox.sh`. That script exports its own
throwaway `HERMES_HOME` and would override the private one. It is meant for
disposable sandboxes:

```bash
../scripts/dev-sandbox.sh npm run dev
../scripts/dev-sandbox.sh --from ~/.hermes-private npm run dev   # seed from private home
```

## How the desktop app picks its backend

Resolution order in `apps/desktop/electron/main.ts`:

1. `HERMES_DESKTOP_HERMES_ROOT`, if it is a Hermes source root.
2. In dev (`npm run dev`), the checkout the app is running from.
3. The canonical install at `$HERMES_HOME/hermes-agent` with its `venv`.
4. A `hermes` command on PATH (skipped with `HERMES_DESKTOP_IGNORE_EXISTING=1`).
5. First-launch bootstrap installer.

Interpreter for a source root: `HERMES_DESKTOP_PYTHON` if set, else
`<root>/.venv/bin/python`, else `<root>/venv/bin/python`, else `python3` on
PATH. The chosen venv's site-packages are mounted and the root goes first on
`PYTHONPATH`.

## Other useful variables

| Variable | Effect |
|---|---|
| `HERMES_HOME` | Home directory for config, `.env`, auth, sessions |
| `HERMES_DESKTOP_HERMES_ROOT` | Python source root for the backend |
| `HERMES_DESKTOP_PYTHON` | Explicit interpreter for the backend |
| `HERMES_DESKTOP_HERMES` | Explicit `hermes` command for step 4 |
| `HERMES_DESKTOP_APP_NAME` | App name, separates single-instance locks |
| `HERMES_DESKTOP_USER_DATA_DIR` | Electron userData directory |
| `HERMES_DESKTOP_CWD` | Initial project directory for chat sessions |
| `HERMES_DESKTOP_WEB_DIST` | Override the renderer dist directory |
