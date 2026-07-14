"""An empty memory collection must announce itself at startup.

Regression guard for the failure this warning exists to catch: when the default
DB path moved under var/, the populated database stayed behind at the old path.
Retrieval kept "working" — every query ran, matched nothing, and the completion
proceeded without context. Nothing in the logs said so. The only symptom was
worse answers.
"""
import logging
from unittest import mock

import pytest

from coding_model_server import memory_service as ms


def _init_with_count(count, caplog, persist_directory="/tmp/does-not-matter"):
    """Construct a MemoryService whose collection reports `count` documents.

    Both heavy dependencies are stubbed: the real ones would download a
    SentenceTransformer and open a ChromaDB on disk.
    """
    fake_collection = mock.Mock()
    fake_collection.count.return_value = count
    fake_client = mock.Mock()
    fake_client.get_or_create_collection.return_value = fake_collection

    with mock.patch.object(ms, "SentenceTransformer", return_value=mock.Mock()), \
         mock.patch.object(ms.chromadb, "PersistentClient", return_value=fake_client), \
         caplog.at_level(logging.INFO, logger=ms.logger.name):
        service = ms.MemoryService(persist_directory=persist_directory)

    return service, caplog.records


def test_empty_collection_warns_loudly(caplog):
    _, records = _init_with_count(0, caplog)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert warnings, "an empty memory collection must produce a WARNING at startup"

    message = warnings[0].getMessage()
    assert "EMPTY" in message
    # The operator needs to know both that retrieval is dead and where to look.
    assert "SILENTLY" in message
    assert "CODING_MODEL_MEMORY_DB" in message
    assert "/tmp/does-not-matter" in message


def test_populated_collection_does_not_warn(caplog):
    _, records = _init_with_count(83_341, caplog)

    assert not [r for r in records if r.levelno >= logging.WARNING]
    assert any("83,341 documents" in r.getMessage() for r in records), \
        "a healthy startup should report the document count it found"


def test_count_failure_does_not_break_startup(caplog):
    """A collection that cannot be counted must not take the server down with it."""
    fake_collection = mock.Mock()
    fake_collection.count.side_effect = RuntimeError("chroma exploded")
    fake_client = mock.Mock()
    fake_client.get_or_create_collection.return_value = fake_collection

    with mock.patch.object(ms, "SentenceTransformer", return_value=mock.Mock()), \
         mock.patch.object(ms.chromadb, "PersistentClient", return_value=fake_client), \
         caplog.at_level(logging.INFO, logger=ms.logger.name):
        service = ms.MemoryService(persist_directory="/tmp/does-not-matter")

    assert service._collection is fake_collection, "startup must still complete"
    assert any("count unavailable" in r.getMessage() for r in caplog.records)
