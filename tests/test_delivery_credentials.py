"""DEV-597 — a configured delivery credential the process cannot read is a
failure that names the path, never a silent downgrade to ambient ssh.

Run 15 delivered nothing because the daemon's unit sandbox hid both the deploy
key and ~/.ssh: the key check read "file does not exist", GIT_SSH_COMMAND was
never set, plain ssh ran under the user's identity, and the run died on host
key verification — three layers away from the actual cause. These tests pin
the two properties that make that impossible: an unreadable credential stops
delivery with its own path in the message, and a usable one carries the pinned
known_hosts so ssh never consults ~/.ssh.
"""
import os

import pytest

from coding_model_autonomous import delivery


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "spec_ws"
    (d / "Sources").mkdir(parents=True)
    (d / "Sources" / "Thing.swift").write_text("struct Thing {}\n")
    return d


@pytest.fixture
def keydir(tmp_path):
    """A credential dir shaped like the deployed one: key + known_hosts."""
    d = tmp_path / "delivery-creds"
    d.mkdir()
    key = d / "deploy_key"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n")
    key.chmod(0o600)
    (d / "known_hosts").write_text("github.com ssh-ed25519 AAAAC3Nz-fake\n")
    return d


def test_missing_key_fails_delivery_naming_the_path(spec_dir, keydir,
                                                    monkeypatch):
    """The sandbox case: configured, but not readable from here."""
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES",
                       "demo=git@github.com:someone/repo.git")
    ghost = keydir / "not_there"
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_SSH_KEY", str(ghost))

    r = delivery.deliver_spec("spec_x", "t", spec_dir,
                              ["Sources/Thing.swift"], "demo", [])

    assert r.status == "failed"
    assert str(ghost) in r.detail
    # The operator must be able to reach the cause without reading the source.
    assert "sandbox" in r.detail.lower()


def test_missing_known_hosts_fails_rather_than_trusting_ambient(spec_dir,
                                                               keydir,
                                                               monkeypatch):
    """A key without its pinned known_hosts would fall back on ~/.ssh."""
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_REMOTES",
                       "demo=git@github.com:someone/repo.git")
    monkeypatch.setenv("AUTONOMOUS_DELIVERY_SSH_KEY",
                       str(keydir / "deploy_key"))
    (keydir / "known_hosts").unlink()

    r = delivery.deliver_spec("spec_x", "t", spec_dir,
                              ["Sources/Thing.swift"], "demo", [])

    assert r.status == "failed"
    assert "known_hosts" in r.detail


def test_unreadable_key_is_caught_by_permission_not_just_existence(keydir):
    """os.path.isfile() alone passes a file the sandbox denies reading."""
    key = keydir / "deploy_key"
    key.chmod(0o000)
    try:
        err = delivery._check_delivery_credentials(str(key))
    finally:
        key.chmod(0o600)

    if os.geteuid() == 0:
        pytest.skip("root reads through mode 000; the check is for the daemon")
    assert err is not None and str(key) in err


def test_usable_credential_pins_known_hosts_in_the_ssh_command(keydir,
                                                              tmp_path):
    """The command git runs must never need ~/.ssh."""
    key = keydir / "deploy_key"
    assert delivery._check_delivery_credentials(str(key)) is None

    # `git var GIT_EDITOR` touches no network and no repo, so the assertion is
    # about the environment _git builds, not about git succeeding.
    captured = {}
    real_run = delivery.subprocess.run

    def spy(cmd, **kw):
        captured.update(kw.get("env", {}))
        return real_run(["true"], capture_output=True, text=True)

    delivery.subprocess.run = spy
    try:
        delivery._git(tmp_path, "var", "GIT_EDITOR", key=str(key))
    finally:
        delivery.subprocess.run = real_run

    cmd = captured["GIT_SSH_COMMAND"]
    assert f"-i {key}" in cmd
    assert f"-o UserKnownHostsFile={keydir / 'known_hosts'}" in cmd
    assert "-o StrictHostKeyChecking=yes" in cmd
    assert "-o BatchMode=yes" in cmd


def test_no_configured_key_still_allows_the_operator_shell_path(spec_dir,
                                                               monkeypatch):
    """An operator rerunning delivery by hand uses their own agent."""
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY", raising=False)
    monkeypatch.delenv("AUTONOMOUS_DELIVERY_SSH_KEY_DEMO", raising=False)

    assert delivery._check_delivery_credentials("") is None
