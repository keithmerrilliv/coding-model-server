# The Mac runner

Swift and Xcode tests cannot run on Linux, so the orchestrator dispatches them
over HTTP to `mac_runner/`, a small FastAPI service on a Mac. This page covers
what it is, the one deploy rule that has bitten more than once, and how to
install and update it.

## The two-host rule

**This repository deploys to two machines, and a pull on one does not deploy
the other.** `mac_runner/*` runs on the Mac and nowhere else. Everything else
runs on the Linux server.

| You changed | Deploy on | With |
| --- | --- | --- |
| `src/`, `systemd/`, `.env` on the server | the Linux server | `bash scripts/redeploy.sh --restart-only` (or `sudo bash scripts/redeploy.sh` for unit-file changes) |
| `mac_runner/` | the Mac | `bash scripts/mac_update_runner.sh --execute`, run on the Mac from the runner's checkout |
| both | both | both, and check the second one |

A fix merged and redeployed on the Linux server is inert on the Mac until the
Mac pulls. DEV-705's timeout raise sat inert for a day while the record said it
was fixed, and DEV-752's runner-side half could not be confirmed at all. So ask
the runner rather than assume:

```sh
set -a; . ~/.config/coding-model-server/.env; set +a
curl -s -H "X-Runner-Key: $MAC_RUNNER_API_KEY" "$MAC_RUNNER_URL/v1/version"
# {"commit": "<sha>", "dirty": false, "timeouts": {...}}
```

`/v1/version` (DEV-805) reports the commit the runner is serving. A 404 means
the runner predates DEV-805 and has not been updated since. The `timeouts`
table matters because the effective budget is the minimum of the runner's and
the caller's: raising a timeout on one host alone changes nothing.

## How a dispatch runs

| Framework | Where it runs | Why |
| --- | --- | --- |
| `swift_test` | on the Mac host, in a fresh git worktree of the registered repo, under the `sandbox-exec` profile `mac_runner/sandbox.sb` | SwiftPM needs no app host. macOS cannot nest sandboxes, so SwiftPM's own manifest sandbox is disabled and the runner's profile confines the whole process tree instead (DEV-294) |
| `xcodebuild_test` | inside a throwaway tart macOS VM, cloned per run and destroyed after it | App-hosted XCTest hangs under `sandbox-exec` whatever the profile (DEV-403); the VM gives it a separate kernel, TCC domain and logged-in session, and no host keychain or files are reachable (DEV-421, DEV-422) |

The VM path needs `CODING_MODEL_RUNNER_SANDBOX` and `CODING_MODEL_RUNNER_VM`
both on (the defaults) and the base image pulled once by hand; the runner never
pulls it. Measured on the Mac Studio: clone ~0 s (APFS copy-on-write),
boot-to-ssh ~19 s, worktree sync ~10 s. A green `swift_test` run therefore says
nothing about VM health; only an `xcodebuild_test` run exercises it.

Every dispatch reads the repo at the `base_ref` the spec names, from the clone
registered in `repos.yml`. The orchestrator logs a warning when a read comes
from a clone that looks stale, but it does not refuse to dispatch. Keep the
registered clones current: `git -C <clone> pull --ff-only` when `main` is
checked out, `git -C <clone> fetch origin main:main` when it is not.

## Install

Once per Mac.

```sh
# 1. Config dir, from the examples.
mkdir -p ~/.config/coding-model-runner && chmod 700 ~/.config/coding-model-runner
cp mac_runner/env.example       ~/.config/coding-model-runner/.env
cp mac_runner/repos.example.yml ~/.config/coding-model-runner/repos.yml
chmod 600 ~/.config/coding-model-runner/.env

# 2. An API key, different from ADMIN_API_KEY. Paste it into
#    CODING_MODEL_RUNNER_API_KEY= in the .env above.
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 3. Register the repos the orchestrator may test (symbolic name -> path).
$EDITOR ~/.config/coding-model-runner/repos.yml

# 4. The full install; a --no-deps client install lacks the runner's deps.
venv/bin/pip install -e .

# 5. For xcodebuild_test: the VM base image (tens of GB, once) and sshpass.
brew trust cirruslabs/cli && brew install cirruslabs/cli/tart
brew install sshpass
tart pull ghcr.io/cirruslabs/macos-tahoe-xcode:26.5   # or CODING_MODEL_RUNNER_VM_IMAGE

# 6. The runner and tunnel LaunchAgents (edit their absolute paths first; see below).
cp mac_runner/com.codingmodel.runner.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codingmodel.runner.plist
brew install autossh
cp mac_runner/com.codingmodel.tunnel.plist ~/Library/LaunchAgents/
ssh <linux-user>@<linux-host> true   # once by hand: seeds known_hosts
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.codingmodel.tunnel.plist
```

**Both plists hardcode absolute paths.** launchd expands neither `~` nor
environment variables, so edit `WorkingDirectory` and `ProgramArguments` to your
checkout before bootstrapping. The tunnel plist also carries a
`LINUX_USER@LINUX_HOST` placeholder and an absolute `autossh` path
(`/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin` on Intel).

On the Linux server, add the matching key to `~/.config/coding-model-server/.env`
and restart the orchestrator:

```
MAC_RUNNER_URL=http://127.0.0.1:5050
MAC_RUNNER_API_KEY=<the value of CODING_MODEL_RUNNER_API_KEY on the Mac>
```

### The tunnel

The runner binds loopback, so an SSH reverse tunnel from the Mac is the only
path to it: `ssh -NT -R 5050:localhost:5050 <linux-user>@<linux-host>`, run
durably by the autossh LaunchAgent. Two settings in the plist are load-bearing:

- `ExitOnForwardFailure=yes`. Without it, a stale tunnel still holding the
  server's 5050 lets ssh connect with a dead forward, and the tunnel looks up
  while the runner is unreachable.
- `AUTOSSH_GATETIME=0`. autossh otherwise gives up for good if the first
  connection dies within 30 s, which is what happens at boot before the network
  is up.

### Verify

From the Linux server:

```sh
curl -s http://127.0.0.1:5050/health            # {"status": "ok"}, liveness only
curl -s -H "X-Runner-Key: $MAC_RUNNER_API_KEY" http://127.0.0.1:5050/v1/repos
curl -s -H "X-Runner-Key: $MAC_RUNNER_API_KEY" http://127.0.0.1:5050/v1/version
```

## Update

On the Mac, from the runner's checkout:

```sh
bash scripts/mac_update_runner.sh            # dry run: prints each command
bash scripts/mac_update_runner.sh --execute  # pull, reclaim leaked VMs, restart
```

Then confirm from the Linux server that `/v1/version` reports the new commit.
`scripts/reclaim_tart_vms.sh` reclaims leaked `cmr-*` VMs on its own; a leaked
VM blocks every `xcodebuild_test` dispatch until it is gone.

## Operate

```sh
launchctl list | grep codingmodel                       # both loaded?
launchctl bootout gui/$(id -u)/com.codingmodel.runner   # stop the runner
tail -f ~/Library/Logs/coding-model-runner.err.log      # runner log
tail -f ~/Library/Logs/coding-model-tunnel.err.log      # tunnel log
tart list                                               # any leaked cmr-* VMs?
```

## Writing a spec for the runner

The spec's test strategy names the repo as registered in `repos.yml`:

```yaml
test_strategy:
  framework: swift_test        # or xcodebuild_test
  required: true
  repo: centipede              # a name in repos.yml
  base_ref: main
  # xcodebuild_test only:
  # scheme: ElectricSheep
  # destination: "platform=macOS"
  # filter: ElectricSheepTests  # without it xcodebuild runs every target, UI tests included
```

`swift_test` takes no scheme and no destination. The full key list is in
[CONFIGURATION.md](CONFIGURATION.md).

## Device leg

The device leg (DEV-850) runs a spec's `xcodebuild_test` suite a second time,
on the physical device attached to the Mac (the Apple Vision Pro on the
Studio), with Metal API validation on. The simulator and the Mac's GPU driver
tolerate Metal misuse that real hardware does not, so a green macOS run can
still hide a crash on the device.

It needs two opt-ins, one per host:

- **The spec** declares `device_destination` in its test strategy, e.g.
  `device_destination: "platform=visionOS"`. The runner finds the attached
  device for that platform and fills in its `id=`.
- **The runner** has `CODING_MODEL_RUNNER_DEVICE_TESTS=1`. It is off by
  default.

The leg runs only in the reviewer phase, only after the macOS run passed, and
the reviewer runs only after a human approved the code at the code-review
gate. That ordering is why the run may be unsandboxed: app-hosted XCTest
cannot run under `sandbox-exec` (DEV-403), and the VM cannot see a USB device.
So model-written test code runs on the Mac host with the runner user's
access. The runner logs a warning naming the device on every such run.

Metal validation is on through `TEST_RUNNER_MTL_DEBUG_LAYER=1`, which
xcodebuild passes to the test process as `MTL_DEBUG_LAYER=1`.

If the leg cannot run, its output starts with `[device-unavailable]` and the
macOS result stands. That covers a runner that is not opted in, no device
attached, a locked or unpaired device, and a runner too old to know the
field. The daemon records a `device_leg_unavailable` anomaly and adds a line
to the release gate saying the device leg did not run. If the leg ran and
failed, a Metal assertion included, the reviewer's tests failed and the
usual retry routing applies. Each leg that ran records a `test_ran` event with
`phase: "device"`.

To enable it on the Mac, add the variable to the runner's env file and restart
the runner:

```sh
echo 'CODING_MODEL_RUNNER_DEVICE_TESTS=1' >> ~/.config/coding-model-runner/.env
launchctl kickstart -k gui/$(id -u)/com.codingmodel.runner
```

Keep the device unlocked and paired while a device run is due.

## Security posture

Two containment mechanisms, one per framework. `swift_test` runs on the host
under the `sandbox-exec` profile (DEV-126), which denies `~/.ssh`, every
Keychain except a named signing keychain, and the runner's own `.env`.
App-hosted XCTest cannot run under that wrapper at all, so `xcodebuild_test`
runs in the throwaway VM, with no host keychain or files reachable and ad-hoc
signing. Setting `CODING_MODEL_RUNNER_SANDBOX=0` turns both off and runs
LLM-written code with the runner user's full access; do not. The one
deliberate exception is the [device leg](#device-leg), which runs unsandboxed
on the host only after human code review, and only on a runner opted in with
`CODING_MODEL_RUNNER_DEVICE_TESTS=1`.
[SECURITY_MIGRATION.md](SECURITY_MIGRATION.md) has the history of these
decisions.
