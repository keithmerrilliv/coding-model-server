"""DEV-839 scope 3 — Config stays the one interface to the roster and prompts.

roster.py and agent_prompts.py define the agents, the model configs and the
interactive prompt texts; config.py binds each of those names onto
``Config``. Everything reads them as ``Config.X``, and tests patch them there.
A module that imported ``roster.AGENTS`` directly would hold a binding that a
``setattr(Config, "AGENTS", ...)`` patch never reaches.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
HOMES = {"roster", "agent_prompts"}


def _imports_a_home(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            last = node.module.rsplit(".", 1)[-1]
            if last in HOMES:
                return True
            if node.module.endswith("coding_model_server") or node.level and node.module == "":
                if any(a.name in HOMES for a in node.names):
                    return True
        if isinstance(node, ast.ImportFrom) and not node.module and \
                any(a.name in HOMES for a in node.names):
            return True
        if isinstance(node, ast.Import) and any(
                a.name.rsplit(".", 1)[-1] in HOMES for a in node.names):
            return True
    return False


def test_only_config_and_the_roster_import_the_definition_modules():
    allowed = {SRC / "coding_model_server" / "config.py",
               SRC / "coding_model_server" / "roster.py"}   # roster uses the prompts
    offenders = [str(p.relative_to(ROOT)) for p in SRC.rglob("*.py")
                 if p not in allowed and _imports_a_home(ast.parse(p.read_text()))]
    assert not offenders, (
        "read the roster and prompt texts through Config, not roster.py or "
        "agent_prompts.py directly:\n" + "\n".join(offenders))


def test_config_binds_the_same_objects():
    from coding_model_server import agent_prompts, roster
    from coding_model_server.config import Config
    # Same objects, not copies: a setitem on Config.AGENTS is a setitem on the
    # roster's dict, which is what the tests that register an agent rely on.
    assert Config.AGENTS is roster.AGENTS
    assert Config.AGENT_ALIASES is roster.AGENT_ALIASES
    assert Config.FEW_SHOT is agent_prompts.FEW_SHOT
    assert Config.EXECUTOR_PROMPT is agent_prompts.EXECUTOR_PROMPT
