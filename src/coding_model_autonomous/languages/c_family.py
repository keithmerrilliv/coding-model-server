"""The C-family pack: C, C++, Objective-C and Objective-C++.

Detection only, for now. It opts into no design rule: a header holds many
types and is named for none of them in particular (LLab's `LSystem.h`
declares `NodeType`, `Texture`, `Node` and `LSystem`), so the
one-type-per-file completeness rule would ask for files the design must not
have (DEV-831).
"""
from __future__ import annotations

from .base import LanguagePack


class CFamilyPack(LanguagePack):
    name = "c_family"
