"""Security gates for tool execution: protected paths, dangerous-command
warnings, hard deny rules, and permission-mode auto-approval.

Pure helpers — they depend only on os/re and the shared permission mode in
``state``. No filesystem mutation, no prompting.
"""
import os
import re
import shlex

from coding_model_server.tool_state import state

# Paths that require explicit user confirmation regardless of permission mode.
#
# Two forms, distinguished by shape:
#   * bare names (".ssh/")      — match ANY path component, at any depth
#   * rooted paths ("/etc/", "~/.aws/") — match that subtree only
#
# Prefer a rooted path unless the directory is genuinely meaningful anywhere.
# ".git" is; "Chrome" is not — as a bare name it would also flag a project
# directory that happens to be called Chrome.
#
# These produce a confirmation prompt, not a hard denial, so the cost of a false
# positive is one keystroke. But prompts that fire constantly train people to
# approve reflexively, so the list stays targeted rather than sweeping: no bare
# "~/Library/" (the runner caches there), no "/var/" (the default workspace lives
# under /var/folders on macOS).
PROTECTED_PATHS = [
    # Version control + key material, meaningful at any depth.
    '.git/', '.ssh/', '.gnupg/',

    # System.
    '/etc/', '/usr/', '/bin/', '/sbin/', '/root/', '/var/db/',

    # macOS secret stores. The Keychain holds signing certs and the iCloud
    # session; the browser stores hold live session cookies and saved passwords.
    # The agent has outbound-fetch tools, so an unprompted read here is the first
    # half of an exfiltration primitive.
    '~/Library/Keychains/',
    '/Library/Keychains/',
    '~/Library/Cookies/',
    '~/Library/Safari/',
    '~/Library/Application Support/Google/Chrome/',
    '~/Library/Application Support/Firefox/',

    # Cloud / registry credentials. Several of these do not exist on this host
    # today; they are listed so they are covered the day they appear, rather than
    # being silently unprotected.
    '~/.aws/', '~/.azure/', '~/.kube/', '~/.docker/', '~/.gem/',
    '~/.config/gcloud/', '~/.password-store/',

    # Linux browser profiles.
    '~/.mozilla/', '~/.config/google-chrome/', '~/.config/chromium/',
]
PROTECTED_FILES = [
    '.env', '.env.local', '.env.production',
    '.bashrc', '.zshrc', '.profile', '.bash_profile',
    'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa',
    'authorized_keys', 'known_hosts',
    '.netrc', '_netrc', '.git-credentials', '.npmrc', '.pypirc', '.htpasswd',
    'credentials', 'credentials.db',
]

# Bare-name entries match any path component. Stored casefolded because
# _is_protected_path folds case on both sides (see the rationale there); these
# are all lowercase already, so the fold is a formality that keeps the two
# sides symmetric.
_PROTECTED_NAMES = tuple(
    p.rstrip('/').casefold() for p in PROTECTED_PATHS if not p.startswith(('/', '~'))
)

# Rooted entries are expanded and resolved once, here, and paired with their
# original (unfolded) label for the message.
#
# The candidate is realpath'd before comparison (so a symlink pointing *at* a
# protected dir is still caught), but the roots used to be compared as written.
# On macOS /etc is itself a symlink to /private/etc, so a realpath'd
# "/etc/passwd" became "/private/etc/passwd" and matched no root — /etc was
# silently unprotected on the very machine the client runs on. Resolving both
# sides fixes it; on Linux realpath("/etc") is just "/etc", so nothing changes.
# The resolved root is stored casefolded to match _is_protected_path's
# case-insensitive comparison.
_PROTECTED_ROOTS = tuple(
    (os.path.realpath(os.path.expanduser(p.rstrip('/'))).casefold(), p)
    for p in PROTECTED_PATHS if p.startswith(('/', '~'))
)


def _is_protected_path(filepath):
    """Check if a path is protected. Returns (is_protected, reason).

    Uses realpath (not just normpath) so a symlink from a benign-looking
    path to a protected one is still caught. Without realpath,
    `/home/user/Dev/symlink → /etc/passwd` would slip the /etc check.
    """
    expanded = os.path.expanduser(filepath)
    try:
        norm = os.path.realpath(expanded)
    except OSError:
        # If the path doesn't exist yet, fall back to the lexical normpath.
        # Real-world: this happens when WRITE_FILE creates a new file.
        norm = os.path.normpath(expanded)

    # Compare case-insensitively. macOS's default filesystem (APFS/HFS+) is
    # case-insensitive and os.path.realpath() does NOT canonicalise case, so
    # "~/.SSH/id_rsa" resolves to a path that still opens the real
    # "~/.ssh/id_rsa" while a case-sensitive component check waves it through —
    # defeating the gate on the platform the client mostly runs on (verified:
    # ~/.SSH/id_rsa_prod, ~/.AWS/config, ~/Library/COOKIES/x all slipped past).
    # Folding both sides can only ADD matches — an exact-case match still
    # matches once folded — so it never weakens protection on a genuinely
    # case-sensitive filesystem; at worst it costs one extra prompt there.
    norm_cf = norm.casefold()
    parts = norm_cf.split(os.sep)
    for name in _PROTECTED_NAMES:
        if name in parts:
            return True, f"inside protected directory '{name}/'"

    for root, label in _PROTECTED_ROOTS:
        # Boundary-aware: a bare startswith(root) also matched "/usrfoo/bar"
        # against "/usr", blocking unrelated paths.
        if norm_cf == root or norm_cf.startswith(root + os.sep):
            return True, f"inside protected directory '{label}'"

    basename = os.path.basename(norm_cf)
    for pfile in PROTECTED_FILES:
        if basename == pfile.casefold():
            return True, f"matches protected file '{pfile}'"
    return False, ""


# Shell patterns that get extra warnings even in yolo mode
DANGEROUS_PATTERNS = [
    # rm with a recursive flag, in any cluster position: -r, -rf, -fr, -Rf, etc.
    # The earlier `-[a-zA-Z]*r` only matched clusters ENDING in r, so the most
    # common form `rm -rf` (ends in f) slipped the warning. Mirror the deny
    # rule's `-[a-zA-Z]*r[a-zA-Z]*` so r anywhere in the cluster is caught.
    (re.compile(r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*|--recursive)\b'), "recursive delete"),
    (re.compile(r'\bsudo\b'), "elevated privileges"),
    (re.compile(r'\bchmod\s+[0-7]*777\b'), "world-writable permissions"),
    (re.compile(r'\bchown\b'), "ownership change"),
    (re.compile(r'\bmkfs\b'), "filesystem format"),
    (re.compile(r'\bdd\s+'), "raw disk write"),
    (re.compile(r'>\s*/dev/(?!null)'), "writing to device"),  # exclude /dev/null
    (re.compile(r'\bgit\s+push\s+.*--force\b'), "force push"),
    (re.compile(r'\bgit\s+reset\s+--hard\b'), "destructive git reset"),
]


def _check_dangerous_command(command):
    """Check if a command matches known dangerous patterns. Returns (is_dangerous, reason)."""
    for pattern, description in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True, description
    return False, ""


# Operations that are always denied — no prompt, no override.
# No $ anchor — catches compound commands like "rm -rf / && echo done".
#
# These are a BACKSTOP, not the security boundary. A denylist can never be
# complete, so nothing may be executed *silently* on the strength of "the
# denylist didn't match" — see _is_auto_approvable_command() for the allow-list
# that actually gates unattended execution.
_RM_R = r'\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+'   # rm with a recursive flag, any cluster order
_ROOTISH = r'(?:~|\$HOME|/home/[^/\s]+|/)'  # a home root or / — not a path inside one

DENY_RULES = [
    # rm -rf / and rm -rf /home  (bare target, end-of-command or a separator)
    (re.compile(_RM_R + r'/\s*(?:[;&|]|$)'), "recursive delete of root"),
    (re.compile(_RM_R + r'/home/?\s*(?:[;&|]|$)'), "recursive delete of /home"),
    # rm -rf /*  and  rm -rf /home/*  — the glob form the old rules missed entirely
    (re.compile(_RM_R + r'/\*'), "recursive delete of root contents"),
    (re.compile(_RM_R + r'/home/\*'), "recursive delete of all home directories"),
    # rm -rf /home/<user>  and  rm -rf ~  — wiping a whole home directory.
    # A path *inside* a home (rm -rf /home/u/proj/build) is NOT denied; it only
    # warns, because that is a legitimate thing to ask an agent to do.
    (re.compile(_RM_R + r'/home/[^/\s]+/?\s*(?:[;&|]|$)'), "recursive delete of a home directory"),
    (re.compile(_RM_R + r'(?:~|\$HOME)/?\s*(?:[;&|]|$)'), "recursive delete of $HOME"),
    # find / -delete  and  find / -exec rm — mass deletion without ever naming `rm` first
    (re.compile(r'\bfind\s+' + _ROOTISH + r'\s.*\s-delete\b'), "mass delete via find -delete"),
    (re.compile(r'\bfind\s+' + _ROOTISH + r'\s.*-exec\s+rm\b'), "mass delete via find -exec rm"),
    # python -c "shutil.rmtree('/')" — recursive delete that never spawns rm
    (re.compile(r'rmtree\s*\(\s*[\'"]' + _ROOTISH), "recursive delete via shutil.rmtree"),
    # Overwrite a raw block device (dd of=/dev/sda, > /dev/nvme0n1)
    (re.compile(r'\bof=/dev/(?:sd|nvme|vd|hd)'), "raw write to a block device"),
    (re.compile(r':\(\)\s*\{\s*:\|:&\s*\};\s*:'), "fork bomb"),
]


def _check_deny_rules(command):
    """Check if a command is unconditionally denied. Returns (denied, reason)."""
    for pattern, description in DENY_RULES:
        if pattern.search(command):
            return True, description
    return False, ""


# ── Allow-list for unattended execution ──────────────────────────────────────
# Commands that may run WITHOUT a prompt when the permission mode auto-approves
# (yolo). Everything not listed here still prompts, even in yolo.
#
# Why an allow-list: the old code auto-approved anything the denylist and the
# dangerous-pattern list failed to match, which made those blocklists the
# security boundary. They cannot be — `find / -delete` and
# `python3 -c "shutil.rmtree('/home/u')"` both sailed through. Inverting it means
# an unknown command is refused a silent run by default, so a gap in the pattern
# lists costs a prompt rather than the filesystem.

# Read-only inspection: cannot mutate the filesystem or reach the network.
# `env` qualifies only in its bare form — with arguments it execs them
# (enforced in _is_auto_approvable_command, like the git subcommand gate).
READONLY_COMMANDS = frozenset({
    'ls', 'pwd', 'cat', 'bat', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep', 'rg',
    'file', 'stat', 'du', 'df', 'tree', 'which', 'whereis', 'type', 'echo', 'printf',
    'date', 'uname', 'whoami', 'id', 'hostname', 'env', 'printenv', 'ps',
    'sort', 'uniq', 'cut', 'tr', 'diff', 'cmp', 'jq', 'column', 'nl',
    'basename', 'dirname', 'realpath', 'readlink', 'md5sum', 'sha256sum',
})

# Build/test/lint tooling. These DO execute project code by design — that is the
# point of an agent loop — but they are not general-purpose code-execution sinks
# the way `python3 -c` / `node -e` / `bash -c` are, which is why those are absent.
BUILD_TEST_COMMANDS = frozenset({
    'pytest', 'tox', 'nox', 'make', 'cmake', 'ninja',
    'npm', 'yarn', 'pnpm', 'tsc', 'eslint', 'prettier', 'vitest', 'jest',
    'cargo', 'go', 'swift', 'xcodebuild', 'gradle', 'mvn',
    'ruff', 'mypy', 'black', 'flake8', 'isort', 'pylint',
})

# `git` is allow-listed only for subcommands that cannot destroy work. Absent by
# design: push, reset, clean, checkout, restore, rebase, filter-branch, gc, prune.
# `config` is here but conditionally: the same subcommand reads AND writes, so
# only its read forms pass (see the gate in _is_auto_approvable_command).
GIT_READONLY_SUBCOMMANDS = frozenset({
    'status', 'log', 'diff', 'show', 'branch', 'remote', 'rev-parse', 'describe',
    'blame', 'ls-files', 'ls-remote', 'tag', 'shortlog', 'grep', 'config',
})

# Flags under which `git config` stays a read. Everything else — write flags
# (--add/--unset/--edit/--remove-section/...), --file/--blob (which point git
# at an arbitrary INI file, e.g. ~/.aws/credentials, and print it), and any
# --flag=value form — falls through to a prompt. Writing without a write flag
# needs a second positional (`git config KEY VALUE`), which the gate also
# rejects, so `git config core.fsmonitor /tmp/evil` cannot slip through as a
# "read".
_GIT_CONFIG_READ_FLAGS = frozenset({
    '--get', '--get-all', '--get-regexp', '--list', '-l',
    '--local', '--global', '--system', '--show-origin', '--show-scope',
    '--name-only', '--null', '-z',
})

# Shell metacharacters. A command containing any of these can chain or redirect
# its way to something the base-binary check never sees (`ls; rm -rf ~`), so it
# never auto-approves — even if the base binary is allow-listed.
_METACHARACTERS = ('|', '&', ';', '$', '`', '\n', '>', '<', '(', ')')


def _auto_approve_allowlist():
    """The set of base commands eligible for silent execution.

    EXTRA_AUTO_APPROVE_COMMANDS (comma-separated) extends it, for projects whose
    build tooling isn't covered above.
    """
    extra = {
        c.strip() for c in os.getenv('EXTRA_AUTO_APPROVE_COMMANDS', '').split(',') if c.strip()
    }
    return READONLY_COMMANDS | BUILD_TEST_COMMANDS | {'git'} | extra


def _argv_protected_path(argv):
    """The first argv token that resolves into a protected path, or None.

    files.py gates READ_FILE against protected paths so `read ~/.ssh/id_rsa`
    can't silently feed an outbound-fetch tool. REMOTE_EXEC never ran that gate,
    so `cat ~/.ssh/id_rsa` — an allow-listed read binary — auto-approved the
    same exfiltration. Re-run the gate here over each argument.

    A `--flag=value` / `VAR=value` token is also checked on its value half, so
    `git config --file=~/.aws/credentials` is examined at the path, not the flag.
    """
    for tok in argv[1:]:
        candidates = [tok]
        if '=' in tok:
            candidates.append(tok.split('=', 1)[1])
        for cand in candidates:
            if not cand or cand.startswith('-'):
                continue
            protected, _ = _is_protected_path(cand)
            if protected:
                return cand
    return None


def _is_auto_approvable_command(command):
    """Whether `command` may run with no prompt. Returns (ok, reason).

    Applied on top of _should_auto_approve(): the permission mode says whether
    unattended execution is allowed at all, this says whether THIS command is
    tame enough for it. Anything unrecognised returns False, so it prompts.
    """
    if not command or not isinstance(command, str):
        return False, "not a command string"

    if any(ch in command for ch in _METACHARACTERS):
        return False, "contains shell metacharacters (could chain or redirect)"

    try:
        argv = shlex.split(command)
    except ValueError as e:
        return False, f"could not be parsed ({e})"
    if not argv:
        return False, "empty command"

    # Only a bare name resolved via PATH may auto-approve. A path form —
    # './pytest', '/tmp/evil/cat', '~/Downloads/x/ls' — names an arbitrary
    # executable that merely shares a basename with an allow-listed binary, so
    # matching it by basename hands silent execution to any file on disk named
    # like `ls` (DEV-347). "VAR=1 pytest" also lands here via the allowlist
    # miss below: argv[0] is "VAR=1", which is not a listed command.
    base = argv[0]
    if base != os.path.basename(base) or base.startswith('~'):
        return False, f"'{argv[0]}' is not a bare command name resolved via PATH"

    allowlist = _auto_approve_allowlist()
    if base not in allowlist:
        return False, f"'{base}' is not in the auto-approve allow-list"

    protected_arg = _argv_protected_path(argv)
    if protected_arg is not None:
        return False, f"argument '{protected_arg}' resolves into a protected path"

    if base == 'git':
        subcommand = next((a for a in argv[1:] if not a.startswith('-')), None)
        if subcommand not in GIT_READONLY_SUBCOMMANDS:
            refused = f"git {subcommand or ''}".strip()
            return False, f"'{refused}' is not a read-only git subcommand"
        if subcommand == 'config':
            rest = argv[argv.index('config') + 1:]
            flags = [a for a in rest if a.startswith('-')]
            positionals = [a for a in rest if not a.startswith('-')]
            if any(f not in _GIT_CONFIG_READ_FLAGS for f in flags) or len(positionals) > 1:
                return False, "'git config' auto-approves only read forms (--get*/--list/one key)"

    if base == 'env' and len(argv) > 1:
        # Bare `env` prints the environment; with anything more it stops being
        # inspection — `env CMD` execs CMD (an unlisted binary rides in on
        # env's allow-list entry), and `env VAR=v CMD` also rewrites the
        # child's environment. Flags are no safer: `-S` smuggles a full
        # command line in one token, `-i`/`-u` exist only to launch something.
        return False, "'env' with arguments launches another command"

    return True, f"'{base}' is auto-approvable"


def _should_auto_approve(tool_name):
    """Check if a tool operation should be auto-approved based on permission mode.

    Args:
        tool_name: One of 'REMOTE_EXEC', 'WRITE_FILE', 'EDIT_FILE', 'READ_FILE', etc.

    Returns:
        bool: True if the operation should be auto-approved.

    REMOTE_EXEC under yolo additionally requires ALLOW_REMOTE_EXEC_YOLO=1
    in the environment. Defense-in-depth: a prompt-injected memory or
    cross-origin CSRF that lands a malicious REMOTE_EXEC must compromise
    yolo mode AND the env opt-in to silently execute. Without the opt-in
    yolo still prompts for shell, even though it auto-approves edits.
    """
    if state.permission_mode == 'yolo':
        if tool_name == 'REMOTE_EXEC':
            return os.getenv('ALLOW_REMOTE_EXEC_YOLO', '').lower() in ('1', 'true', 'yes')
        return True
    if state.permission_mode == 'acceptEdits':
        # Auto-approve everything EXCEPT shell commands
        return tool_name != 'REMOTE_EXEC'
    return False  # 'default' — prompt for everything
