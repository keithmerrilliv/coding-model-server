"""DEV-834: a tighter relevance ceiling, and a record of WHICH documents were injected.

Measured 2026-09-27 over the 54 recorded retrievals: the best hit ranged
0.459-0.600, and replaying the spec-title queries showed every hit at 0.52 or
above was off-topic (SwiftUI `Scene` initializers for an Obj-C USD scene
loader; `cancelAction`, a keyboard shortcut, for "cancel MLX generation"). The
events carried only `hits` and `best_distance`, so that judgement needed a
replay of every query.
"""
import importlib

from coding_model_autonomous.executor import accumulate_agent_fields, agent_event_fields
from coding_model_server import memory_service as ms
from coding_model_server.routes import chat


def test_default_ceiling_is_the_measured_noise_edge(monkeypatch):
    monkeypatch.delenv("MEMORY_RELEVANCE_THRESHOLD", raising=False)
    try:
        assert importlib.reload(ms).MEMORY_RELEVANCE_THRESHOLD == 0.52
    finally:
        importlib.reload(ms)


def test_memory_title_is_the_heading_or_first_line():
    assert ms.memory_title("# init(content:)\n\nKind: init\n\nCreates a group") == "init(content:)"
    assert ms.memory_title("\n  Metal renderers which have a winding order.\nmore") \
        == "Metal renderers which have a winding order."
    assert ms.memory_title("") == ""
    assert len(ms.memory_title("# " + "x" * 200)) == 80


def _service_returning(memories):
    svc = ms.MemoryService.__new__(ms.MemoryService)
    svc.search_memory = lambda query, n_results=5: memories
    return svc


def test_stats_name_each_injected_document():
    svc = _service_returning([
        {"document": "# presentationFrameIndex\n\nKind: property", "distance": 0.4721},
        {"document": "# Immersive spaces\n\nKind: API Collection", "distance": 0.5003},
    ])
    stats: dict = {}
    text = svc.get_context_string("frame lifecycle", stats=stats)
    assert "presentationFrameIndex" in text
    assert stats["sources"] == [
        {"title": "presentationFrameIndex", "distance": 0.472},
        {"title": "Immersive spaces", "distance": 0.5},
    ]
    assert stats["hits"] == 2 and stats["best_distance"] == 0.4721


def test_sources_list_only_what_fit_the_budget():
    """A hit the character budget cut is not in the prompt, so not in sources."""
    svc = _service_returning([
        {"document": "# first\n" + "a" * 100, "distance": 0.46},
        {"document": "# second\n" + "b" * 5000, "distance": 0.47},
    ])
    stats: dict = {}
    svc.get_context_string("q", max_tokens=100, stats=stats)
    assert [s["title"] for s in stats["sources"]] == ["first"]


def test_sources_ride_the_outcome_onto_the_event():
    outcome: dict = {}
    sources = [{"title": "presentationFrameIndex", "distance": 0.472}]
    chat._record_rag(outcome, "injected", agent="architect", hits=1,
                     best_distance=0.472, sources=sources)
    assert outcome["sources"] == sources
    out = agent_event_fields({"agent": "architect", "rag": {**outcome, "gate": "retrieved"}})
    assert out["rag"]["sources"] == sources


def test_empty_outcome_carries_no_sources_key():
    """Negative control: nothing injected, nothing named."""
    outcome: dict = {}
    chat._record_rag(outcome, "empty", agent="architect", hits=0)
    assert "sources" not in outcome


def test_attempt_tally_keeps_the_closest_hit_and_every_source():
    tally: dict = {}
    a = {"title": "init(content:)", "distance": 0.479}
    b = {"title": "defaultLaunchBehavior(_:)", "distance": 0.527}
    accumulate_agent_fields(tally, {"rag": {"outcome": "injected", "hits": 1,
                                            "best_distance": 0.527, "sources": [b]}})
    accumulate_agent_fields(tally, {"rag": {"outcome": "injected", "hits": 2,
                                            "best_distance": 0.479, "sources": [a, b]}})
    rag = tally["rag"]
    assert rag["best_distance"] == 0.479
    assert rag["sources"] == [b, a]
    assert rag["hits"] == 3 and rag["calls"] == 2
