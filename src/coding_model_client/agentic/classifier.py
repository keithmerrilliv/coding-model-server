"""Client-side heuristic query classifier.

Classifies user queries into types that determine retrieval strategy
and budget. Pure keyword heuristic — no model call required.
"""
import re
from enum import Enum


class QueryType(Enum):
    LOCATE = "LOCATE"        # "where is X?"
    EXPLAIN = "EXPLAIN"      # "how does X work?"
    DEBUG = "DEBUG"           # "why does X fail?"
    IMPLEMENT = "IMPLEMENT"  # "build X"
    REFACTOR = "REFACTOR"    # "change X to Y"
    GENERAL = "GENERAL"      # fallback


# Budget limits per query type (max tool iterations)
QUERY_BUDGETS = {
    QueryType.LOCATE: 8,
    QueryType.EXPLAIN: 20,
    QueryType.DEBUG: 30,
    QueryType.IMPLEMENT: 80,   # Implementation tasks need many productive iterations
    QueryType.REFACTOR: 40,
    QueryType.GENERAL: 25,
}

# Priority-ordered patterns — first match wins
_PATTERNS = [
    (QueryType.LOCATE, re.compile(
        r'(?:where\s+(?:is|are|does|do)|find\s+the|locate\b|which\s+file|what\s+file'
        r'|path\s+to|where.*?\b(?:defined|declared|implemented|lives?))',
        re.IGNORECASE
    )),
    (QueryType.DEBUG, re.compile(
        r'(?:why\s+(?:does|is|do|did|are).*?(?:fail|crash|error|break|hang|slow|wrong)'
        r'|\berror\b|\bcrash(?:es|ed|ing)?\b|\bbug\b|\bfix\b|\bbroken\b'
        r'|(?:not|doesn\'t|isn\'t|won\'t)\s+work'
        r'|\bdebug\b|\b500\b|\b404\b|\bexception\b|\btraceback\b|\bfailing\b)',
        re.IGNORECASE
    )),
    (QueryType.IMPLEMENT, re.compile(
        r'(?:build\b|create\b|implement\w*\b|write\s+(?:a\s+)?(?:function|class|module|script|test)'
        r'|add\s+(?:a\s+)?(?:feature|support|endpoint|handler|command|tool)'
        r'|make\s+(?:a\s+)?(?:function|class|script|tool)\s+that'
        r'|continue\s+(?:implement|build|work)'
        r'|finish\s+(?:implement|build|the\s+work|the\s+remaining))',
        re.IGNORECASE
    )),
    (QueryType.REFACTOR, re.compile(
        r'(?:refactor\b|rename\b|move\s+.*?\bto\b|split\b|extract\b'
        r'|reorganize\b|clean\s*up\b|modularize\b|restructure\b'
        r'|change\s+.*?\bto\b|convert\s+.*?\bto\b|migrate\b)',
        re.IGNORECASE
    )),
    (QueryType.EXPLAIN, re.compile(
        r'(?:how\s+does|explain\b|what\s+is|describe\b|walk\s+me\s+through'
        r'|understand\b|architecture\b|how\s+.*?\bwork)',
        re.IGNORECASE
    )),
]


def classify_query(user_input: str) -> QueryType:
    """Classify a user query into a QueryType.

    Returns the first matching type, or GENERAL if no pattern matches.
    """
    for query_type, pattern in _PATTERNS:
        if pattern.search(user_input):
            return query_type
    return QueryType.GENERAL
