"""Discovery of what this particular Mac can offer a test run.

Kept separate from frameworks.py so the command builders stay pure and
testable: this module shells out, frameworks.py only formats arguments.

Two things are discovered, and they are related — a physical device will only
accept a properly signed build, so device testing implies a real identity:

  * a code-signing identity, preferred over the ad-hoc "-" fallback (DEV-395)
  * an attached physical device, preferred over a simulator (DEV-396)
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Optional

logger = logging.getLogger("mac_runner.environment")

_DISCOVERY_TIMEOUT = 20

# Ordered best-first. A distribution cert can sign a local test build, but a
# development cert is the right tool and avoids burning a distribution profile.
_IDENTITY_PREFERENCE = (
    "Apple Development",
    "Mac Developer",
    "iPhone Developer",
    "Developer ID Application",
    "Apple Distribution",
)

# "  1) A1B2C3 "Apple Development: Keith Merrill (TEAMID)""
_IDENTITY_RE = re.compile(r'^\s*\d+\)\s+([0-9A-F]{40})\s+"(.+)"\s*$', re.M)
# Trailing "(TEAMID)" inside the identity's common name.
_TEAM_RE = re.compile(r"\(([A-Z0-9]{10})\)\s*$")


def _run(cmd: list[str]) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=_DISCOVERY_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("discovery command %s failed: %s", cmd[0], e)
        return None
    if r.returncode != 0:
        logger.info("discovery command %s exited %d", cmd[0], r.returncode)
    return (r.stdout or "") + (r.stderr or "")


def find_signing_identity() -> tuple[str, Optional[str]]:
    """Return (identity, team) to hand xcodebuild.

    Falls back to ("-", None) — ad-hoc — when the Mac holds no usable
    certificate. Ad-hoc still satisfies the Apple Silicon kernel, so a build
    runs either way; a real identity additionally allows device install.
    """
    out = _run(["security", "find-identity", "-v", "-p", "codesigning"])
    if not out:
        return "-", None

    found = _IDENTITY_RE.findall(out)
    if not found:
        logger.info("no code-signing identity in keychain — ad-hoc signing")
        return "-", None

    for wanted in _IDENTITY_PREFERENCE:
        for _sha, name in found:
            if name.startswith(wanted):
                team = m.group(1) if (m := _TEAM_RE.search(name)) else None
                # Return the GENERIC name ("Apple Development"), not the
                # fully-qualified common name. Xcode projects default to
                # automatic signing, and handing xcodebuild a specific
                # certificate fails every target in the graph with
                # "<target> has conflicting provisioning settings. <target> is
                # automatically signed, but code signing identity ... has been
                # manually specified" — including every SwiftPM dependency.
                # The generic form is what automatic signing expects.
                logger.info("using signing identity %r (from %r, team=%s)",
                            wanted, name, team)
                return wanted, team

    name = found[0][1]
    team = m.group(1) if (m := _TEAM_RE.search(name)) else None
    generic = name.split(":", 1)[0].strip() or name
    logger.info("using signing identity %r (from %r, team=%s)",
                generic, name, team)
    return generic, team


# "Keith's Vision Pro (2.0) (00008112-001A2B3C0123456E)" under "== Devices =="
_DEVICE_RE = re.compile(r"^(.*?)\s+\(([\d.]+)\)\s+\(([0-9A-Za-z-]{8,})\)\s*$")

# Which xcodebuild platform name each spec platform maps to for a real device.
_DEVICE_PLATFORM = {
    "ios": "iOS",
    "ios simulator": "iOS",
    "xros": "visionOS",
    "visionos": "visionOS",
    "visionos simulator": "visionOS",
    "tvos": "tvOS",
    "watchos": "watchOS",
}


def _platform_of(destination: str) -> str:
    for part in destination.split(","):
        k, _, v = part.partition("=")
        if k.strip().lower() == "platform":
            return v.strip().lower()
    return ""


def find_attached_device(destination: str) -> Optional[tuple[str, str, str]]:
    """Find a physical device matching `destination`'s platform.

    Returns (name, platform, udid) or None. macOS destinations return None:
    "the device" for a macOS test IS this Mac, so the default destination is
    already correct and there is nothing to prefer.
    """
    platform = _platform_of(destination)
    if not platform or platform.startswith("macos"):
        return None

    target = _DEVICE_PLATFORM.get(platform)
    if target is None:
        return None

    out = _run(["xcrun", "xctrace", "list", "devices"])
    if not out:
        return None

    # Physical hardware is listed under "== Devices ==" and stops at the next
    # section header; simulators live under their own heading and must not be
    # picked up here — preferring one over the other is the whole point.
    in_devices = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("=="):
            in_devices = stripped.lower().startswith("== devices")
            continue
        if not in_devices or not stripped:
            continue
        m = _DEVICE_RE.match(stripped)
        if not m:
            continue
        name, _version, udid = m.groups()
        # This Mac appears in the Devices list too; it is not a target here.
        if "mac" in name.lower() and target != "macOS":
            continue
        logger.info("attached %s device: %s (%s)", target, name, udid)
        return name, target, udid

    logger.info("no attached %s device — using the requested destination", target)
    return None


def resolve_environment(opts: dict) -> dict:
    """Overlay discovered signing/destination onto a run's options.

    Explicit values from the plan always win: if the spec pinned a
    destination id or named an identity, that intent is respected.
    """
    resolved = dict(opts)

    # Destination first: whether we are targeting hardware decides how to sign.
    destination = resolved.get("destination") or "platform=macOS"
    targeting_device = "id=" in destination
    if not targeting_device:
        device = find_attached_device(destination)
        if device is not None:
            name, platform, udid = device
            resolved["destination"] = f"platform={platform},id={udid}"
            resolved["destination_device"] = name
            targeting_device = True

    if resolved.get("signing_identity"):
        return resolved

    identity, team = find_signing_identity()
    resolved["signing_identity"] = identity

    if not targeting_device:
        # MANUAL style with the real certificate. Both other options put UI on
        # the runner's screen, which defeats a headless runner:
        #   unsigned  -> "is damaged ... move it to the Trash"  (DEV-395)
        #   ad-hoc    -> "unidentified developer, open anyway?" (DEV-397)
        # A build signed by a real Apple Development cert is trusted on the Mac
        # that owns that cert, so nothing is prompted. Manual style is what
        # keeps Xcode out of provisioning — Automatic is what produced
        # "No Accounts: Add a new account in Accounts settings" on a Mac with
        # no Apple ID in Xcode. A macOS test bundle needs no profile, so the
        # certificate alone is sufficient.
        resolved["signing_style"] = "Manual"
        if identity == "-":
            logger.warning(
                "no code-signing identity in the keychain — falling back to "
                "ad-hoc, which will raise an 'unidentified developer' prompt "
                "on this Mac's screen")
        return resolved

    # A physical device additionally needs a provisioning profile, which does
    # require an Apple ID signed into Xcode on this Mac.
    resolved["signing_style"] = "Automatic"
    if team and not resolved.get("development_team"):
        resolved["development_team"] = team
    if identity == "-":
        logger.warning(
            "device run selected but no signing identity is available; "
            "the install will be rejected")
    return resolved
