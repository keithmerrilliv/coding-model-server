"""The Swift pack: rules, prechecks, normalizers and test counting for Swift
code built by SwiftPM (`swift_test`) or Xcode (`xcodebuild_test`)."""
from __future__ import annotations

from typing import Optional

from ..base import (
    RULE_COMPLETENESS,
    RULE_EQUATABLE,
    RULE_TUPLE_CONFORMANCE,
    RULE_VALUE_MUTABILITY,
    LanguagePack,
)
from . import counting, prechecks, rules, symbols
from . import normalize as fixes


def _default_isolation(test_strategy: dict) -> Optional[str]:
    """The target's `SWIFT_DEFAULT_ACTOR_ISOLATION`, as the operator's
    test_strategy declares it (`default_actor_isolation: MainActor`), or None
    (DEV-784). Electric Sheep's app target is default-isolated; nothing in a
    served file says so."""
    v = test_strategy.get("default_actor_isolation")
    return str(v) if v else None


class SwiftPack(LanguagePack):
    name = "swift"
    frameworks = frozenset({"swift_test", "xcodebuild_test"})
    # Equatable and tuple conformance are Swift's protocol rules; completeness
    # rests on Swift's one-type-per-eponymous-file convention; a value type's
    # `mutating` contract is Swift's value semantics.
    design_rules = frozenset({RULE_EQUATABLE, RULE_TUPLE_CONFORMANCE,
                              RULE_COMPLETENESS, RULE_VALUE_MUTABILITY})
    # A Swift module is one namespace: two files each declaring `struct Foo`
    # fail to build together even in different directories.
    shares_target_namespace = True

    def standing_rules(self, paths: list[str]) -> str:
        return rules.SWIFT_RULES

    def target_rules(self, test_strategy: dict) -> str:
        return rules.render_default_isolation_rule(
            _default_isolation(test_strategy))

    def fix_hint(self, message: str) -> Optional[str]:
        return rules.fix_hint(message)

    def run_prechecks(self, files, context_files, test_strategy):
        return prechecks.run_swift_prechecks(
            files, context_files,
            default_isolation=_default_isolation(test_strategy))

    def declared_types(self, content: str) -> set[str]:
        return fixes.declared_top_level_types(content)

    def provided_symbols(self, source: str) -> tuple[frozenset[str], tuple[str, ...]]:
        return symbols.provided_symbols(source)

    def count_tests(self, source: str) -> int:
        return counting.count_swift_tests(source)

    def normalize(self, path: str, content: str) -> tuple[str, Optional[str]]:
        if not path.endswith(".swift"):
            return content, None
        new_content, needed = fixes._ensure_foundation_import(content)
        if not needed:
            return content, None
        return new_content, (f"{path}: added `import Foundation` "
                             f"(references {', '.join(needed)})")
