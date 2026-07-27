"""Per-framework command builders for the mac runner."""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Per-framework default timeouts (seconds). Cold xcodebuild can be slow;
# swift test is faster but still heavier than pytest.
DEFAULT_TIMEOUTS: dict[str, int] = {
    "swift_test": 300,
    "xcodebuild_test": 900,
}


class FrameworkError(ValueError):
    pass


def build_swift_test_cmd(worktree: Path, **opts: Any) -> list[str]:
    # --disable-sandbox stops SwiftPM applying ITS OWN sandbox to manifest
    # evaluation. macOS cannot nest sandboxes: with our sandbox-exec wrapper
    # already applied, SwiftPM's inner sandbox_apply fails with EPERM and the
    # run dies before compiling anything (DEV-294). This does not widen what
    # the build can reach — our profile still confines the whole process tree.
    cmd = ["swift", "test", "--parallel", "--disable-sandbox"]
    if filt := opts.get("filter"):
        cmd.extend(["--filter", filt])
    return cmd


def _only_testing_args(opts: dict[str, Any]) -> list[str]:
    """Translate `filter` into xcodebuild's -only-testing: selectors.

    Without this, `xcodebuild test -scheme X` runs EVERY test target in the
    scheme. Every Xcode app template ships a UI-test target alongside the unit
    tests, and a UI test needs to launch the app against a window server — under
    the runner it dies with "Early unexpected exit ... Test crashed with signal
    kill before establishing connection", failing the whole run no matter how
    good the unit tests are (DEV-394). `filter` was already accepted by the
    server and honoured for `swift test`, but silently ignored here.

    Accepts one selector or a comma/space separated list, each either a bare
    target ("ElectricSheepTests") or a narrower path
    ("ElectricSheepTests/ForcingStrategyTests/testMaskZeroesNonTopK").
    """
    raw = opts.get("filter")
    if not raw:
        return []
    selectors = [s for s in str(raw).replace(",", " ").split() if s]
    args: list[str] = []
    for sel in selectors:
        args.extend(["-only-testing:" + sel])
    return args


def build_xcodebuild_test_cmd(worktree: Path, derived_data: Path, **opts: Any) -> list[str]:
    scheme = opts.get("scheme")
    if not scheme:
        raise FrameworkError("xcodebuild_test requires 'scheme'")
    destination = opts.get("destination", "platform=macOS")
    configuration = opts.get("configuration", "Debug")
    cmd = [
        "xcodebuild", "test",
        "-scheme", scheme,
        "-destination", destination,
        "-configuration", configuration,
        "-derivedDataPath", str(derived_data),
        # Resolution happens in a separate, UNSANDBOXED pre-step (see
        # build_resolve_cmd / DEV-294) because SwiftPM sandboxes manifest
        # evaluation itself and macOS cannot nest sandboxes. Disabling it here
        # keeps the sandboxed step from trying again and failing with EPERM.
        "-disableAutomaticPackageResolution",
        # The SECOND place the toolchain sandboxes itself: swift-frontend runs
        # macro plugins under sandbox-exec, which nests exactly like SwiftPM's
        # manifest sandbox and fails the same way —
        #   External macro implementation type '...' could not be found;
        #   'swift-plugin-server' produced malformed response
        # with sandbox_apply EPERM underneath. Any dependency using a macro
        # (mlx-swift's @TaskLocal here) breaks the build. $(inherited) so a
        # project's own OTHER_SWIFT_FLAGS survive being overridden here.
        "OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox",
    ]
    # Signing. The binary MUST be signed: on Apple Silicon the kernel refuses
    # to exec unsigned arm64 code, so CODE_SIGNING_ALLOWED=NO got the xctest
    # host SIGKILLed on launch and made Gatekeeper raise the "is damaged ...
    # move it to the Trash" dialog on the runner's desktop (DEV-395).
    # environment.resolve_environment supplies a real identity when the Mac
    # holds one — required to install on a physical device — and otherwise
    # ad-hoc "-", which satisfies the kernel with no certificate or keychain.
    identity = opts.get("signing_identity") or "-"
    cmd += [
        "CODE_SIGNING_ALLOWED=YES",
        "CODE_SIGNING_REQUIRED=NO",
        f"CODE_SIGN_IDENTITY={identity}",
    ]
    if team := opts.get("development_team"):
        cmd.append(f"DEVELOPMENT_TEAM={team}")
        # Let Xcode mint/select a profile rather than demanding a pinned one.
        cmd.append("-allowProvisioningUpdates")
    else:
        cmd += ["CODE_SIGN_ENTITLEMENTS=", "DEVELOPMENT_TEAM=",
                "PROVISIONING_PROFILE_SPECIFIER="]
    cmd.extend(_project_selector(opts))
    cmd.extend(_only_testing_args(opts))
    return cmd


def _project_selector(opts: dict[str, Any]) -> list[str]:
    """-workspace/-project flags, or nothing to let xcodebuild auto-detect."""
    if ws := opts.get("workspace"):
        return ["-workspace", ws]
    if project := opts.get("project"):
        return ["-project", project]
    return []


def build_resolve_cmd(framework: str, worktree: Path, derived_data: Path,
                      **opts: Any) -> "list[str] | None":
    """Command that resolves package dependencies, to run OUTSIDE the sandbox.

    DEV-294: the runner wraps builds in sandbox-exec (DEV-126), but SwiftPM
    spawns its own sandbox-exec to evaluate Package.swift manifests. macOS does
    not permit nesting — the inner sandbox_apply returns EPERM — so every
    xcodebuild against a project with SwiftPM dependencies died during
    "Resolve Package Graph", before compiling a line. That is all three
    registered repos.

    Splitting the work is what makes both halves possible:

      * resolution runs unsandboxed, so SwiftPM's own manifest sandbox applies
        normally. Manifest evaluation IS code execution, so it matters that it
        stays confined by something — here it is SwiftPM's sandbox rather than
        ours.
      * the build/test step, which runs the LLM-authored patch, stays inside
        our profile. That is the code DEV-126 exists to contain, and it is
        unaffected by this change.

    Returns None when the framework needs no separate resolve step.
    """
    if framework == "xcodebuild_test":
        scheme = opts.get("scheme")
        if not scheme:
            raise FrameworkError("xcodebuild_test requires 'scheme'")
        return [
            "xcodebuild", "-resolvePackageDependencies",
            "-scheme", scheme,
            "-derivedDataPath", str(derived_data),
            *_project_selector(opts),
        ]
    if framework == "swift_test":
        # swift_test passes --disable-sandbox, so SwiftPM never nests and the
        # build resolves inline. No pre-step needed.
        return None
    return None


def build_cmd(framework: str, worktree: Path, derived_data: Path, **opts: Any) -> list[str]:
    if framework == "swift_test":
        return build_swift_test_cmd(worktree, **opts)
    if framework == "xcodebuild_test":
        return build_xcodebuild_test_cmd(worktree, derived_data, **opts)
    raise FrameworkError(f"unsupported framework: {framework}")


SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def wrap_sandbox(cmd: list[str], *, profile: Path, worktree: Path,
                 derived_data: Path, home: "Path | None" = None) -> list[str]:
    """Prefix *cmd* with a sandbox-exec invocation of *profile* (DEV-126).

    The profile confines LLM-authored build/test code: credential paths are
    unreadable and $HOME is read-only outside the worktree / DerivedData /
    build caches. Parameters are passed with -D so custom worktree and
    DerivedData locations stay writable.
    """
    home = home or Path.home()
    return [
        SANDBOX_EXEC, "-f", str(profile),
        "-D", f"HOME={home}",
        "-D", f"WORKTREE={worktree}",
        "-D", f"DERIVED_DATA={derived_data}",
        *cmd,
    ]
