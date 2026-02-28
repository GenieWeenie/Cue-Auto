"""Tests for optional VectorMemory semantic store."""

from __future__ import annotations

from types import SimpleNamespace

from cue_agent.config import CueConfig
from cue_agent.memory.vector_memory import VectorMemory


def test_vector_memory_disabled_is_noop():
    vm = VectorMemory(CueConfig(vector_memory_enabled=False))

    vm.add_turn("chat-1", "user", "hello")
    assert vm.is_available is False
    assert vm.recall("chat-1", "hello") == ""
    assert vm.recall_as_context("chat-1", "hello") == ""


def test_vector_memory_enabled_without_chromadb_degrades(monkeypatch):
    def _missing(_name: str):
        raise ModuleNotFoundError("chromadb not installed")

    monkeypatch.setattr("cue_agent.memory.vector_memory.import_module", _missing)

    vm = VectorMemory(CueConfig(vector_memory_enabled=True))
    assert vm.is_available is False
    assert vm.recall("chat-1", "hello") == ""


def test_vector_memory_add_and_recall_with_fake_chromadb(monkeypatch):
    stores: dict[str, list[dict]] = {}

    class _FakeCollection:
        def __init__(self, rows: list[dict]):
            self._rows = rows

        def add(self, ids, documents, metadatas):  # noqa: ANN001
            for doc_id, doc, metadata in zip(ids, documents, metadatas):
                self._rows.append(
                    {
                        "id": doc_id,
                        "doc": doc,
                        "metadata": metadata,
                    }
                )

        def query(self, query_texts, n_results, where):  # noqa: ANN001
            del query_texts
            chat_id = where.get("chat_id")
            docs = [row["doc"] for row in self._rows if row["metadata"].get("chat_id") == chat_id]
            return {"documents": [docs[:n_results]]}

        def get(self, where=None, include=None, limit=None):  # noqa: ANN001
            del include
            filtered = list(self._rows)
            if where:
                for key, value in where.items():
                    filtered = [r for r in filtered if r["metadata"].get(key) == value]
            if limit is not None:
                filtered = filtered[:limit]
            return {
                "ids": [r["id"] for r in filtered],
                "documents": [r["doc"] for r in filtered],
                "metadatas": [r["metadata"] for r in filtered],
            }

        def delete(self, ids):  # noqa: ANN001
            id_set = set(ids)
            self._rows[:] = [row for row in self._rows if row["id"] not in id_set]

    class _FakeClient:
        def __init__(self, path: str):  # noqa: ARG002
            self._path = path

        def get_or_create_collection(self, name: str):  # noqa: ARG002
            key = f"{self._path}:{name}"
            rows = stores.setdefault(key, [])
            return _FakeCollection(rows)

    fake_chromadb = SimpleNamespace(PersistentClient=_FakeClient)
    monkeypatch.setattr("cue_agent.memory.vector_memory.import_module", lambda _name: fake_chromadb)

    vm = VectorMemory(
        CueConfig(
            vector_memory_enabled=True,
            vector_memory_path="tmp/chroma",
            vector_memory_collection="test_collection",
            vector_memory_top_k=3,
        )
    )
    assert vm.is_available is True

    vm.add_turn("chat-a", "user", "remember alpha")
    vm.add_turn("chat-a", "assistant", "remember beta")
    vm.add_turn("chat-b", "user", "remember gamma")

    recalled = vm.recall("chat-a", "remember", limit=5)
    assert "- remember alpha" in recalled
    assert "- remember beta" in recalled
    assert "gamma" not in recalled

    context = vm.recall_as_context("chat-a", "remember", limit=2)
    assert "Long-term semantic memory" in context


def test_vector_memory_consolidates_old_entries(monkeypatch):
    stores: dict[str, list[dict]] = {}

    class _FakeCollection:
        def __init__(self, rows: list[dict]):
            self._rows = rows

        def add(self, ids, documents, metadatas):  # noqa: ANN001
            for doc_id, doc, metadata in zip(ids, documents, metadatas):
                self._rows.append({"id": doc_id, "doc": doc, "metadata": metadata})

        def query(self, query_texts, n_results, where):  # noqa: ANN001
            del query_texts
            chat_id = where.get("chat_id")
            docs = [row["doc"] for row in self._rows if row["metadata"].get("chat_id") == chat_id]
            return {"documents": [docs[:n_results]]}

        def get(self, where=None, include=None, limit=None):  # noqa: ANN001
            del include
            filtered = list(self._rows)
            if where:
                for key, value in where.items():
                    filtered = [r for r in filtered if r["metadata"].get(key) == value]
            if limit is not None:
                filtered = filtered[:limit]
            return {
                "ids": [r["id"] for r in filtered],
                "documents": [r["doc"] for r in filtered],
                "metadatas": [r["metadata"] for r in filtered],
            }

        def delete(self, ids):  # noqa: ANN001
            id_set = set(ids)
            self._rows[:] = [row for row in self._rows if row["id"] not in id_set]

    class _FakeClient:
        def __init__(self, path: str):  # noqa: ARG002
            self._path = path

        def get_or_create_collection(self, name: str):
            key = f"{self._path}:{name}"
            rows = stores.setdefault(key, [])
            return _FakeCollection(rows)

    fake_chromadb = SimpleNamespace(PersistentClient=_FakeClient)
    monkeypatch.setattr("cue_agent.memory.vector_memory.import_module", lambda _name: fake_chromadb)

    vm = VectorMemory(
        CueConfig(
            vector_memory_enabled=True,
            vector_memory_path="tmp/chroma",
            vector_memory_collection="test_collection",
        )
    )

    for i in range(5):
        vm.add_turn("chat-z", "user", f"entry-{i}")

    deleted = vm.consolidate_chat(
        "chat-z",
        summarizer=lambda _chat, items: "SUMMARY:" + ", ".join(items),
        min_entries=4,
        keep_recent=2,
        max_items=50,
    )
    assert deleted == 3

    recalled = vm.recall("chat-z", "entry", limit=20)
    assert "SUMMARY:" in recalled
    assert "entry-3" in recalled
    assert "entry-4" in recalled
    assert "- entry-0\n" not in recalled
    assert not recalled.startswith("- entry-0")


def test_vector_memory_recall_survives_restart_with_persistent_store(monkeypatch):
    stores: dict[str, list[dict]] = {}

    class _FakeCollection:
        def __init__(self, rows: list[dict]):
            self._rows = rows

        def add(self, ids, documents, metadatas):  # noqa: ANN001
            for doc_id, doc, metadata in zip(ids, documents, metadatas):
                self._rows.append({"id": doc_id, "doc": doc, "metadata": metadata})

        def query(self, query_texts, n_results, where):  # noqa: ANN001
            del query_texts
            chat_id = where.get("chat_id")
            docs = [row["doc"] for row in self._rows if row["metadata"].get("chat_id") == chat_id]
            return {"documents": [docs[:n_results]]}

        def get(self, where=None, include=None, limit=None):  # noqa: ANN001
            del where, include, limit
            return {"ids": [], "documents": [], "metadatas": []}

        def delete(self, ids):  # noqa: ANN001
            del ids

    class _FakeClient:
        def __init__(self, path: str):
            self._path = path

        def get_or_create_collection(self, name: str):
            key = f"{self._path}:{name}"
            rows = stores.setdefault(key, [])
            return _FakeCollection(rows)

    fake_chromadb = SimpleNamespace(PersistentClient=_FakeClient)
    monkeypatch.setattr("cue_agent.memory.vector_memory.import_module", lambda _name: fake_chromadb)

    cfg = CueConfig(
        vector_memory_enabled=True,
        vector_memory_path="persist/path",
        vector_memory_collection="persist_collection",
    )
    vm1 = VectorMemory(cfg)
    vm1.add_turn("chat-r", "user", "remember persistent note")

    # Simulated process restart: create a fresh instance with same backing path+collection.
    vm2 = VectorMemory(cfg)
    recalled = vm2.recall("chat-r", "persistent", limit=5)
    assert "remember persistent note" in recalled
