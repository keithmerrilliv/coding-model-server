"""The interface every language pack implements.

Every hook has a neutral default, so a pack overrides only what its language
needs. A hook that returns nothing leaves every prompt and every file set
byte-identical to a pipeline without the pack: that is how a spec in one
language pays nothing for another language's rules.
"""
from __future__ import annotations

from typing import Optional, Protocol

# Design rules (design_testability) a language opts into. A rule runs on a
# design only when EVERY language the design allocates files in opts in, so a
# rule built on one language's conventions never judges another's files.
RULE_EQUATABLE = "equatable"                  # comparisons need Equatable types
RULE_TUPLE_CONFORMANCE = "tuple_conformance"  # tuples conform to no protocol
RULE_COMPLETENESS = "completeness"            # one type per eponymous file
RULE_SEAM_IMPORTS = "seam_imports"            # seams import the code under test


class PrecheckResult(Protocol):
    """What a pack's prechecks return; the daemon reads nothing else."""
    violations: list

    def failed(self) -> bool: ...
    def summary(self) -> str: ...
    def report(self) -> str: ...
    def event_payload(self) -> list[dict]: ...


class LanguagePack:
    """One language's knowledge, behind the hooks the pipeline calls."""

    #: The pack's name, as ``languages.PACKS`` keys it.
    name: str = ""
    #: The test frameworks that build and test this language's code.
    frameworks: frozenset[str] = frozenset()
    #: The design rules this language opts into (the ``RULE_*`` names).
    design_rules: frozenset[str] = frozenset()
    #: True when every file of a target shares one namespace, so two
    #: attempts' same-named files cannot both be kept in the workspace.
    shares_target_namespace: bool = False

    def standing_rules(self, paths: list[str]) -> str:
        """A prompt section for a file set that includes this language."""
        return ""

    def target_rules(self, test_strategy: dict) -> str:
        """A prompt section the operator's test_strategy switches on."""
        return ""

    def fix_hint(self, message: str) -> Optional[str]:
        """The one edit a compiler diagnostic *message* asks for."""
        return None

    def run_prechecks(self, files: list[tuple[str, str]],
                      context_files: list[tuple[str, str]],
                      test_strategy: dict) -> Optional[PrecheckResult]:
        """Errors decidable without a toolchain, or None for no checks."""
        return None

    def declared_types(self, content: str) -> set[str]:
        """Type names a source file declares at file scope."""
        return set()

    def count_tests(self, source: str) -> int:
        """Test declarations in *source*."""
        return 0

    def normalize(self, path: str, content: str) -> tuple[str, Optional[str]]:
        """A deterministic boilerplate fix: ``(content, note)``, where a None
        note means the file was left unchanged."""
        return content, None
