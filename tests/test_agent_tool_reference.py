"""The agent TOOL_REFERENCE must only advertise tools that work.

<<<CUPERTINO>>> sat in this list for months telling every implementer, architect
and reviewer it could search Apple docs, while shelling out to a binary present
on no machine and uninstallable as documented (DEV-479). An agent that took the
offer burned a turn on a guaranteed error.

The APPLE_DEEP_DOCS entry had the opposite failure: real service, but it named
no tools, so the model had to guess and every guess returned "Unknown tool"
(DEV-484).

These are cheap structural guards against both shapes recurring.
"""
import re

import pytest

from coding_model_server.config import Config


def _reference_text():
    for attr in ("TOOL_REFERENCE", "GIT_TOOL_REFERENCE"):
        val = getattr(Config, attr, None)
        if val:
            return "\n".join(val) if isinstance(val, (list, tuple)) else str(val)
    pytest.skip("TOOL_REFERENCE not found on Config")


# Tools verified to return CONTENT, by calling every one of the MCP's 15
# through /v1/tools/apple_deep_docs. Only these four do.
WORKING = {
    "search_swift_evolution",       # structured proposals
    "get_swift_evolution_proposal", # full proposal detail
    "fetch_apple_documentation",    # parsed developer.apple.com page
    "fetch_github_file",            # actual file contents
}

# Present on the MCP but useless to an agent. Three groups, all verified:
#   - return {"search_urls": ...} or a bare URL, never text: search_swift_repos,
#     search_apple_online, get_framework_info, the WWDC pair, the HIG pair
#   - read Xcode's on-disk docs, so [] on the Linux host: the four *_docs /
#     *_document / get_xcode_versions tools
# Naming any of them points an agent at a guaranteed empty, which is the turn
# <<<CUPERTINO>>> used to waste.
NOT_AGENT_USABLE = {
    "search_swift_repos", "search_apple_online", "get_framework_info",
    "search_wwdc_notes", "get_wwdc_session",
    "search_human_interface_guidelines", "list_human_interface_guidelines_platforms",
    "search_docs", "get_document", "list_documents", "get_xcode_versions",
}


class TestNoDeadTools:
    def test_cupertino_is_gone(self):
        """It cannot work on any machine here; advertising it costs a turn."""
        assert "CUPERTINO" not in _reference_text()

    def test_no_unusable_deep_docs_tool_is_advertised(self):
        text = _reference_text()
        named = {t for t in NOT_AGENT_USABLE if t in text}
        assert not named, f"these return nothing an agent can use: {sorted(named)}"


class TestDeepDocsIsUsable:
    def test_the_tag_is_still_offered(self):
        assert "APPLE_DEEP_DOCS" in _reference_text()

    def test_it_names_tools_rather_than_a_placeholder(self):
        """A bare {"tool":"NAME"} template is what made this unusable."""
        text = _reference_text()
        named = WORKING & set(re.findall(r"[a-z_]{6,}", text))
        assert named, ("APPLE_DEEP_DOCS names no concrete tool — an agent must "
                       "guess, and every wrong guess returns 'Unknown tool'")

    def test_the_evolution_tools_are_named(self):
        """Swift Evolution is the one corpus the scraped RAG cannot cover, so
        it is the specific reason to keep this MCP at all."""
        assert "search_swift_evolution" in _reference_text()


class TestUpdaterIsWiredUp:
    def test_updater_exists_and_is_executable(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        script = root / "scripts" / "update_deep_docs_mcp.sh"
        assert script.is_file(), "the MCP updater is missing"
        assert script.stat().st_mode & 0o111, "systemd ExecStart needs it executable"

    def test_updater_rolls_back_on_smoke_failure(self):
        """The rollback is the whole reason auto-pulling third-party code is
        acceptable; assert it did not get dropped."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        body = (root / "scripts" / "update_deep_docs_mcp.sh").read_text()
        assert "reset --hard" in body, "no rollback path"
        assert "--ff-only" in body, "a diverging upstream must not be merged unattended"

    def test_timers_do_not_collide(self):
        """The docs refresh runs Sun 02:00 for hours; this must not overlap."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        mcp = (root / "systemd" / "coding-model-mcp-update.timer").read_text()
        docs = (root / "systemd" / "coding-model-apple-docs-refresh.timer").read_text()
        assert "02:00:00" in docs and "04:00:00" in mcp
