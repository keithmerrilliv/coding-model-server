"""Regression guards for the src/ package restructure.

Two import bugs survived the move to the ``src/`` layout because nothing
imported the affected paths under test:

  1. ``compaction.py`` used a module-level ``_SESSION`` that was never defined
     (NameError at call time, not import time).
  2. ``memory_service.py`` used a bare ``from code_chunker import …`` that can't
     resolve inside the ``coding_model_server`` package; the ImportError was swallowed,
     silently disabling code chunking.

These tests generalise each bug to its whole class so the next instance is
caught anywhere in the tree, plus a broad import-smoke over the packages.
"""
import ast
import importlib
import importlib.util
import os
import pkgutil

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _iter_py_files():
    for root, _dirs, files in os.walk(SRC):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


# --- Bug class 1: a module that USES `_SESSION.` must DEFINE `_SESSION` --------

def _uses_session(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "_SESSION":
                return True
    return False


def _defines_or_imports_session(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_SESSION":
                    return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name) == "_SESSION":
                    return True
    return False


def test_modules_using_session_define_it():
    offenders = []
    for path in _iter_py_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        if _uses_session(tree) and not _defines_or_imports_session(tree):
            offenders.append(os.path.relpath(path, SRC))
    assert not offenders, f"_SESSION used without a module-level definition/import in: {offenders}"


# --- Bug class 2: code_chunker must resolve as a package member ----------------

def test_code_chunker_importable_as_package():
    # The bad `from code_chunker import` could never resolve this.
    mod = importlib.import_module("coding_model_server.code_chunker")
    assert hasattr(mod, "CodeChunker")


def test_memory_service_chunker_enabled_when_dep_present():
    import coding_model_server.memory_service as ms
    has_tree_sitter = importlib.util.find_spec("tree_sitter_languages") is not None
    if not has_tree_sitter:
        pytest.skip("tree_sitter_languages not installed; chunker legitimately disabled")
    assert ms._chunker is not None, (
        "code_chunker import is broken again — _chunker is None despite "
        "tree_sitter_languages being installed (src/ layout import regression)"
    )


# --- Broad import smoke -------------------------------------------------------

_FIRST_PARTY = ("coding_model_server", "coding_model_client", "coding_model_autonomous")


def _all_modules():
    for pkg in _FIRST_PARTY:
        spec = importlib.util.find_spec(pkg)
        if spec is None or not spec.submodule_search_locations:
            continue
        for info in pkgutil.walk_packages(spec.submodule_search_locations, pkg + "."):
            yield info.name


@pytest.mark.parametrize("modname", sorted(_all_modules()))
def test_module_imports(modname):
    # `python -m pkg` entrypoints run main()/argparse at import — importing them
    # under pytest is meaningless (and exits the interpreter), so skip them.
    if modname.endswith(".__main__"):
        pytest.skip("module is a __main__ entrypoint")
    try:
        importlib.import_module(modname)
    except ModuleNotFoundError as e:
        missing = (e.name or "").split(".")[0]
        # A missing first-party module is a real bug (e.g. a mistyped intra-
        # package import like the src/ layout regressions). A missing
        # third-party dependency just means it isn't installed here — skip.
        if missing in _FIRST_PARTY:
            raise
        pytest.skip(f"optional/uninstalled dependency: {missing}")
