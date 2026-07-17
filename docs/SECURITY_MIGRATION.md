# Security Hardening — Manual Migration Steps

Companion to the hardening patches applied to `src/coding_model_server/server.py`,
the start scripts, the four `.service` units, and
`src/coding_model_autonomous/executor.py`. The code changes alone are not
enough — the steps below must be run on every host that runs the server, the
client, or the orchestrator.

Two hosts are assumed:

- **Mac dev machine** — `/Users/km4/Dev/qwen-server` (you run the client here).
- **Linux server** — `/home/keith-merrill/Dev/coding-model-server` (runs the
  server, orchestrator, and monitor via systemd).

Note the two checkouts sit at *different* directory names: the Mac was never
renamed when the project was. The systemd units and `scripts/monitor_resources.py`
correctly hardcode the Linux path; the Mac LaunchAgent plist did not, and was
fixed separately.

---

## 0. Install the packages (both hosts)

The repo uses a PyPA `src/` layout: `coding_model_server`, `coding_model_client`,
and `coding_model_autonomous` all live under `src/`. Nothing is importable until
the project is installed into the venv, so `python -m coding_model_client` (and
the server / orchestrator equivalents) fail with `ModuleNotFoundError` on a fresh
checkout.

```sh
# Linux server — full server dependency set.
cd ~/Dev/coding-model-server && venv/bin/pip install -e .

# Mac dev machine — client only. --no-deps avoids building llama-cpp-python,
# transformers, and chromadb, none of which the client needs. The client runs
# on requests + python-dotenv; rich and beautifulsoup4 are optional extras.
cd /Users/km4/Dev/qwen-server && venv/bin/pip install -e . --no-deps
```

Verify:

```sh
venv/bin/python -c "import coding_model_client; print(coding_model_client.__file__)"
```

---

## 1. Move `.env` out of the repo (both hosts)

Secrets should live outside the repo tree so they can't leak via
`git add -A`, `rsync`, or a tarball of the working directory. The start
scripts and systemd units now prefer `~/.config/coding-model-server/.env`.

### On the Mac dev machine

```sh
mkdir -p ~/.config/coding-model-server
chmod 700 ~/.config/coding-model-server
mv /Users/km4/Dev/qwen-server/.env ~/.config/coding-model-server/.env
chmod 600 ~/.config/coding-model-server/.env
```

### On the Linux server

```sh
mkdir -p ~/.config/coding-model-server
chmod 700 ~/.config/coding-model-server
mv /home/keith-merrill/Dev/coding-model-server/.env ~/.config/coding-model-server/.env
chmod 600 ~/.config/coding-model-server/.env
```

The repo-local `.env` is still read as a fallback with a warning — delete it
once the new location is verified working.

Note the systemd units use `EnvironmentFile=-` (leading dash), which *tolerates*
a missing file. Put the `.env` at the wrong path and the server starts with no
`ADMIN_API_KEY` rather than failing loudly — see step 2.

---

## 2. Rotate `ADMIN_API_KEY`

The existing key lived in the repo directory and should be treated as
compromised. Generate a fresh one:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Edit `~/.config/coding-model-server/.env` on **both hosts** and replace the
`ADMIN_API_KEY=` line with the new value. The key must match between server
and client.

The server now refuses to start if `ADMIN_API_KEY` is empty. For local-dev
only, you can opt out with `CODING_MODEL_ALLOW_UNAUTH=1` — do not set this on
any host reachable from a network.

---

## 3. Install bubblewrap (Linux server only)

Required to sandbox the LLM-generated tests the orchestrator runs.

```sh
# Debian / Ubuntu
sudo apt install bubblewrap

# Fedora / RHEL
sudo dnf install bubblewrap

# Arch
sudo pacman -S bubblewrap
```

Verify:

```sh
which bwrap && bwrap --version
```

If `bwrap` is missing the orchestrator will refuse to run tests. To opt out
(not recommended), set `CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1` in
`~/.config/coding-model-server/.env`.

### jest note

If you use `framework: jest` in any spec and your `node`/`npx` lives under
an nvm-managed path in `$HOME`, it will be invisible inside the sandbox
(because `/home` is masked with tmpfs). Install node system-wide
(`/usr/bin/node` or `/usr/local/bin/node`) or opt out for that host.

---

## 4. Reload systemd and restart services (Linux server)

The service units changed (new EnvironmentFile path, hardening directives,
`coding-model-monitor` no longer running as root).

```sh
# User-scoped units (coding-model-server, coding-model-orchestrator)
systemctl --user daemon-reload
systemctl --user restart coding-model-server coding-model-orchestrator
systemctl --user status  coding-model-server coding-model-orchestrator

# coding-model-monitor — adjust command below based on whether it's a user or
# system unit on your host. It's shown as a system unit in the repo copy.
sudo systemctl daemon-reload
sudo systemctl restart coding-model-monitor
sudo systemctl status  coding-model-monitor
```

Check logs for either service if startup fails:

```sh
journalctl --user -u coding-model-server -n 100 --no-pager
journalctl --user -u coding-model-orchestrator -n 100 --no-pager
sudo journalctl -u coding-model-monitor -n 100 --no-pager
```

---

## 5. Grant non-root monitor access to RAPL counters (Linux server)

`coding-model-monitor.service` now runs as `keith-merrill` instead of root. It
needs read access to the Intel RAPL energy counters under `/sys/class/powercap/`.

Check current permissions:

```sh
ls -la /sys/class/powercap/intel-rapl:0/energy_uj
```

If it's root-only (the default), grant read access via POSIX ACL — this
survives reboots on most distros:

```sh
sudo setfacl -m u:keith-merrill:r /sys/class/powercap/intel-rapl:0/energy_uj
# Repeat for any other counters monitor_resources.py reads. List candidates:
ls /sys/class/powercap/*/energy_uj
```

Alternatively, put the file's group in a group the user already belongs to
and `chmod g+r`.

If you prefer to keep the monitor running as root, revert just the
`User=keith-merrill` line in `coding-model-monitor.service` back to `User=root`.

---

## 6. Verify

On the Linux server:

```sh
# Server is bound to loopback (unless you opted into 0.0.0.0).
ss -lntp | grep 5000

# Auth is enforced — this should return 401.
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/v1/memory

# With the new key it should succeed.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-Admin-Key: $(grep ^ADMIN_API_KEY ~/.config/coding-model-server/.env | cut -d= -f2-)" \
  http://127.0.0.1:5000/v1/memory

# Orchestrator can invoke the sandbox — run a trivial spec through the
# executing state and check the log for `running tests via bwrap`.
journalctl --user -u coding-model-orchestrator -n 200 --no-pager | grep 'running tests via'
```

On the Mac dev machine:

```sh
# Client can reach the server with the new key. Set CODING_MODEL_SERVER_IP in
# ~/.config/coding-model-server/.env first — it defaults to 192.168.50.101.
./bin/start-client.sh
```

A clean start prints `Connected to <your server IP>` and no
`Warning: loading .env from repo`.

---

## Reference — what changed in code

| Area | File | Change |
|---|---|---|
| Bind address | `src/coding_model_server/server.py` | `HOST` default → `127.0.0.1` |
| Auth enforcement | `src/coding_model_server/server.py` | Refuses startup with empty `ADMIN_API_KEY` unless `CODING_MODEL_ALLOW_UNAUTH=1` |
| Shell auto-run | `src/coding_model_client/config.py` | `ALLOW_SHELL_MODE` default → `false` |
| Env loading | `bin/start.sh`, `bin/start-client.sh` | Prefer `~/.config/coding-model-server/.env`; repo `.env` is a fallback with warning |
| Systemd env path | `coding-model-server.service`, `coding-model-orchestrator.service` | `EnvironmentFile=-%h/.config/coding-model-server/.env` added |
| Systemd hardening | all four `.service` files | `PrivateTmp`, `ProtectKernel*`, `LockPersonality`, `ProtectSystem=strict`, `ReadWritePaths` |
| Monitor privileges | `coding-model-monitor.service` | `User=root` → `User=keith-merrill` |
| Test sandbox | `src/coding_model_autonomous/executor.py` | LLM-generated pytest/jest wrapped in `bwrap` (no network, no `/home`, no env inheritance); opt-out via `CODING_MODEL_ALLOW_UNSANDBOXED_TESTS=1` |
| Docs | `.env.example`, `docs/CONFIGURATION.md`, `docs/TUTORIAL.md` | Updated defaults, documented new flags and env path |

---

## 7. Set up the Mac runner (Mac only)

Swift and Xcode tests can't run on Linux, so the orchestrator dispatches
them over HTTP to a `mac_runner` service on your Mac. Code lives at
`mac_runner/` in this repo.

### Risk acknowledgement

The Mac runner executes LLM-generated Swift **without a sandbox**. On your
primary workstation, that means the test process can read your Keychain
(including signing certs and iCloud session), `~/Documents`, `~/.ssh`,
browser profiles, etc. You chose this trade-off over the weaker protection
macOS's `sandbox-exec` would provide. Options to harden later:

- Create a dedicated `coding-model-runner` macOS user with no iCloud/Keychain
  and run the LaunchAgent as that user (strongest practical isolation on Mac).
- Move to a dedicated Mac mini builder when available.

### One-time setup on the Mac

```sh
# 1. Create config dir and copy the examples in.
mkdir -p ~/.config/coding-model-runner && chmod 700 ~/.config/coding-model-runner
cp /Users/km4/Dev/qwen-server/mac_runner/env.example         ~/.config/coding-model-runner/.env
cp /Users/km4/Dev/qwen-server/mac_runner/repos.example.yml   ~/.config/coding-model-runner/repos.yml
chmod 600 ~/.config/coding-model-runner/.env

# 2. Generate an API key (DIFFERENT from ADMIN_API_KEY).
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
#    Paste it into CODING_MODEL_RUNNER_API_KEY= in ~/.config/coding-model-runner/.env.

# 3. Register the repos you want the orchestrator to test.
$EDITOR ~/.config/coding-model-runner/repos.yml

# 4. Make sure the venv has the runner deps installed (see step 0).
cd /Users/km4/Dev/qwen-server && venv/bin/pip install -e .

# 5. Install the LaunchAgent (auto-start on login).
cp mac_runner/com.codingmodel.runner.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codingmodel.runner.plist
# Or for a dev session without LaunchAgent:
./bin/start-mac-runner.sh

# 6. Install the tunnel LaunchAgent — the runner binds loopback, so without this
#    the orchestrator has no route to it. See "Transport" below for the details.
brew install autossh
cp mac_runner/com.codingmodel.tunnel.plist ~/Library/LaunchAgents/
ssh keith-merrill@linux-server true    # once by hand: seeds ~/.ssh/known_hosts
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codingmodel.tunnel.plist

# 7. Verify from the Linux box — should list your repos.yml entries:
#    curl -s http://127.0.0.1:5050/health
```

> **Both plists hardcode absolute paths.** The runner's `WorkingDirectory` and
> `ProgramArguments` point at `/Users/km4/Dev/qwen-server` — LaunchAgent plists
> expand neither `~` nor environment variables, so if your checkout lives
> elsewhere (or your username isn't `km4`) edit those two keys before
> bootstrapping, or the agent will fail to start.
>
> The tunnel plist likewise hardcodes `keith-merrill@192.168.1.64`, its log paths
> under `/Users/km4`, and `/opt/homebrew/bin/autossh` (that path is Apple
> Silicon; Intel Homebrew installs to `/usr/local/bin/autossh`). launchd does not
> search `PATH` for `argv[0]`, so that one must be absolute and correct.
>
> The runner also needs `fastapi`, `uvicorn`, `pydantic`, and `pyyaml`, which a
> `--no-deps` client install (step 0) does **not** provide. Use the full
> `venv/bin/pip install -e .` on any Mac that actually runs the runner.

Verify:

```sh
curl -s http://127.0.0.1:5050/health | python3 -m json.tool
# expect: {"status":"ok","repos":["character-sync", ...]}
```

### One-time setup on the Linux server

Copy the matching API key into `~/.config/coding-model-server/.env`:

```
MAC_RUNNER_URL=http://127.0.0.1:5050
MAC_RUNNER_API_KEY=<same value as CODING_MODEL_RUNNER_API_KEY on the Mac>
```

Restart:

```sh
systemctl --user restart coding-model-orchestrator
```

### Transport: SSH reverse tunnel (recommended)

The runner binds loopback, so this tunnel is the *only* path to it:

```sh
ssh -NT -R 5050:localhost:5050 keith-merrill@linux-server
```

This makes the Mac runner reachable at `http://127.0.0.1:5050` on the
Linux box, end-to-end over SSH — no LAN exposure, no TLS to manage. With the
tunnel down, every dispatch fails with `mac-runner unreachable at
http://127.0.0.1:5050`.

Run it by hand only for a one-off. For anything durable use the autossh
LaunchAgent template at `mac_runner/com.codingmodel.tunnel.plist`, which
survives reboots, sleep/wake, and Wi-Fi drops:

```sh
brew install autossh
cp mac_runner/com.codingmodel.tunnel.plist ~/Library/LaunchAgents/
# Connect once by hand first, so the host key is in ~/.ssh/known_hosts —
# BatchMode means the agent can never answer a host-key prompt.
ssh keith-merrill@linux-server true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codingmodel.tunnel.plist
```

Two settings in it are load-bearing, and both fail in ways that are easy to
misread:

* `ExitOnForwardFailure=yes` — without it, if the server's 5050 is still held by
  a stale tunnel, ssh connects happily with a **dead forward**. The tunnel looks
  up while the runner is unreachable. With it, ssh exits loudly and autossh
  retries until the stale forward is reaped.
* `AUTOSSH_GATETIME=0` — autossh's default is to give up permanently if its first
  connection dies inside 30s, which is exactly what happens at login or boot
  before the network is up. `0` disables that and retries forever.

### LaunchAgent management

```sh
# Start / stop — runner
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codingmodel.runner.plist
launchctl bootout   gui/$(id -u)/com.codingmodel.runner

# Start / stop — tunnel
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codingmodel.tunnel.plist
launchctl bootout   gui/$(id -u)/com.codingmodel.tunnel

# Is either actually up?  (a missing Label means it is not loaded)
launchctl list | grep codingmodel

# Logs
tail -f ~/Library/Logs/coding-model-runner.out.log ~/Library/Logs/coding-model-runner.err.log
tail -f ~/Library/Logs/coding-model-tunnel.out.log ~/Library/Logs/coding-model-tunnel.err.log
```

### Writing a spec that exercises the runner

The planner's `test_strategy` block in spec YAML should look like:

```yaml
test_strategy:
  framework: swift_test           # or xcodebuild_test
  required: true
  repo: character-sync            # must match a name in repos.yml
  base_ref: main
  # xcodebuild_test only:
  # scheme: Character-SYNC
  # destination: "platform=iOS Simulator,name=iPhone 15,OS=latest"
```

See `docs/CONFIGURATION.md` → "Planner `test_strategy` block" for the full key
list.

---

## What remains open

- **Shell execution**: `src/coding_model_server/tool_handlers/shell.py` still uses `subprocess.run(..., shell=True)` gated by a regex deny-list. The default is now off (`ALLOW_SHELL_MODE=false`), but turning it on still exposes the class of bypasses called out in the audit. A parsed-allowlist rewrite is the proper fix.
- **Shell is not workspace-confined**: `WRITE_FILE` / `EDIT_FILE` are hard-confined to the workspace (see `CODING_MODEL_WORKSPACE` in `docs/CONFIGURATION.md`), and shell commands *run* with the workspace as their CWD — but that is a default, not a jail. With `shell=True` a command can still `cd` out and write anywhere the user can. Confining it needs the same sandboxing the item above calls for.
- **Scraping SSRF**: `scraping/*` and `ingest_url_content` accept arbitrary URLs and follow redirects. Not addressed in this pass.
- **Rate limiting**: still none. A misbehaving client can DoS the server even when authenticated.
- **PDF ingest size cap**: `INGEST_MAX_FILE_SIZE` is declared in `.env.example` but not read in code.
- **Mac runner has no sandbox**: Swift test code executes with the runner user's privileges. Revisit if/when a dedicated builder exists.
- **`EnvironmentFile=-` fails open**: the systemd units tolerate a missing env file by design (the leading dash). If the `.env` is ever moved or mistyped, the server starts with an empty `ADMIN_API_KEY` instead of failing loudly. The startup guard added to `server.py` catches this (it refuses to boot unauthenticated), but the units themselves will not complain — check `journalctl` after any change to the env path.
- **pbxproj editing for Xcode projects without existing XCTest targets**: not supported yet — the planner must target projects that already have a test target. SPM (`swift_test`) handles add-new-tests fine via text edits to `Package.swift`.
- **Binary patches**: the orchestrator ships UTF-8 files only; binary assets (images, asset catalogs) can't currently be added/modified by the LLM. Worktree bases carry whatever binaries exist at `base_ref`.
