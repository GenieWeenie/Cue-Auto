"""Optional long-term semantic memory backed by ChromaDB."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Callable
from uuid import uuid4

from cue_agent.config import CueConfig

logger = logging.getLogger(__name__)


class VectorMemory:
    """Store and retrieve semantically related conversation snippets."""

    def __init__(self, config: CueConfig):
        self._enabled = config.vector_memory_enabled
        self._top_k = max(1, config.vector_memory_top_k)
        self._collection: Any | None = None
        self._available = False

        if not self._enabled:
            return

        try:
            chromadb = import_module("chromadb")
            client = chromadb.PersistentClient(path=config.vector_memory_path)
            self._collection = client.get_or_create_collection(name=config.vector_memory_collection)
            self._available = True
            logger.info(
                "Vector memory enabled",
                extra={
                    "event": "vector_memory_enabled",
                    "path": config.vector_memory_path,
                    "collection": config.vector_memory_collection,
                },
            )
        except Exception as exc:
            logger.warning(
                "Vector memory unavailable; continuing without semantic recall",
                extra={
                    "event": "vector_memory_unavailable",
                    "error": str(exc),
                },
            )

    @property
    def is_available(self) -> bool:
        return self._enabled and self._available and self._collection is not None

    def add_turn(self, chat_id: str, role: str, content: str, run_id: str | None = None) -> None:
        """Add one conversation turn to semantic memory."""
        if not content.strip():
            return
        self.add_entry(
            chat_id=chat_id,
            content=content,
            source=f"turn:{role}",
            run_id=run_id,
        )

    def add_entry(
        self,
        chat_id: str,
        content: str,
        source: str,
        run_id: str | None = None,
    ) -> None:
        """Add arbitrary text content for semantic retrieval."""
        if not self.is_available:
            return
        if not content.strip():
            return

        assert self._collection is not None
        try:
            doc_id = f"{chat_id}:{datetime.now(timezone.utc).timestamp()}:{uuid4().hex[:8]}"
            metadata: dict[str, str] = {
                "chat_id": chat_id,
                "source": source,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
            if run_id:
                metadata["run_id"] = run_id
            self._collection.add(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata],
            )
        except Exception:
            logger.exception(
                "Failed to add vector memory entry",
                extra={
                    "event": "vector_memory_add_failed",
                    "chat_id": chat_id,
                    "source": source,
                },
            )

    def list_chat_ids(self) -> list[str]:
        """List known chat IDs currently present in vector memory."""
        if not self.is_available:
            return []

        assert self._collection is not None
        try:
            result = self._collection.get(include=["metadatas"])
        except Exception:
            logger.exception(
                "Failed to enumerate vector memory chats",
                extra={"event": "vector_memory_list_chats_failed"},
            )
            return []

        metadatas = result.get("metadatas", [])
        if not isinstance(metadatas, list):
            return []

        ids: set[str] = set()
        for meta in metadatas:
            if isinstance(meta, dict):
                chat_id = meta.get("chat_id")
                if isinstance(chat_id, str) and chat_id:
                    ids.add(chat_id)
        return sorted(ids)

    def consolidate_all(
        self,
        summarizer: Callable[[str, list[str]], str] | None = None,
        *,
        min_entries: int,
        keep_recent: int,
        max_items: int,
    ) -> dict[str, int]:
        """Consolidate all known chats and return summary stats."""
        consolidated_chats = 0
        deleted_entries = 0
        for chat_id in self.list_chat_ids():
            deleted = self.consolidate_chat(
                chat_id=chat_id,
                summarizer=summarizer,
                min_entries=min_entries,
                keep_recent=keep_recent,
                max_items=max_items,
            )
            if deleted > 0:
                consolidated_chats += 1
                deleted_entries += deleted

        return {
            "consolidated_chats": consolidated_chats,
            "deleted_entries": deleted_entries,
        }

    def consolidate_chat(
        self,
        chat_id: str,
        summarizer: Callable[[str, list[str]], str] | None = None,
        *,
        min_entries: int,
        keep_recent: int,
        max_items: int,
    ) -> int:
        """Summarize old entries for one chat and compact them."""
        if not self.is_available:
            return 0

        rows = self._get_chat_rows(chat_id=chat_id, max_items=max_items)
        if len(rows) < max(1, min_entries):
            return 0

        non_summary_rows = [r for r in rows if not r["source"].startswith("summary:")]
        if len(non_summary_rows) <= keep_recent:
            return 0

        compact_rows = non_summary_rows[: len(non_summary_rows) - keep_recent]
        if not compact_rows:
            return 0

        summary_input = [r["document"] for r in compact_rows if r["document"]]
        if not summary_input:
            return 0

        summary = self._build_summary(chat_id=chat_id, snippets=summary_input, summarizer=summarizer)
        if summary:
            self.add_entry(
                chat_id=chat_id,
                content=summary,
                source="summary:consolidated",
            )

        to_delete = [r["id"] for r in compact_rows if r["id"]]
        if not to_delete:
            return 0

        assert self._collection is not None
        try:
            self._collection.delete(ids=to_delete)
        except Exception:
            logger.exception(
                "Failed to compact vector memory entries",
                extra={
                    "event": "vector_memory_compaction_failed",
                    "chat_id": chat_id,
                    "entry_count": len(to_delete),
                },
            )
            return 0
        return len(to_delete)

    def recall_as_context(self, chat_id: str, query: str, limit: int | None = None) -> str:
        """Return semantically relevant snippets as a prompt-ready context block."""
        recalls = self.recall(chat_id=chat_id, query=query, limit=limit)
        if not recalls:
            return ""
        return f"Long-term semantic memory:\n{recalls}"

    def recall(self, chat_id: str, query: str, limit: int | None = None) -> str:
        """Recall semantically similar snippets for a chat."""
        if not self.is_available:
            return ""
        if not query.strip():
            return ""

        assert self._collection is not None
        k = max(1, limit or self._top_k)

        try:
            result = self._collection.query(
                query_texts=[query],
                n_results=k,
                where={"chat_id": chat_id},
            )
        except Exception:
            logger.exception(
                "Failed to query vector memory",
                extra={
                    "event": "vector_memory_query_failed",
                    "chat_id": chat_id,
                },
            )
            return ""

        raw_docs = result.get("documents", [])
        docs = raw_docs[0] if raw_docs and isinstance(raw_docs[0], list) else raw_docs

        unique_snippets: list[str] = []
        seen: set[str] = set()
        for doc in docs:
            if not isinstance(doc, str):
                continue
            snippet = " ".join(doc.split())
            if not snippet or snippet in seen:
                continue
            seen.add(snippet)
            unique_snippets.append(f"- {snippet}")

        return "\n".join(unique_snippets)

    def _build_summary(
        self,
        chat_id: str,
        snippets: list[str],
        summarizer: Callable[[str, list[str]], str] | None = None,
    ) -> str:
        if not snippets:
            return ""

        summary = ""
        if summarizer is not None:
            try:
                summary = summarizer(chat_id, snippets).strip()
            except Exception:
                logger.exception(
                    "Vector memory summarizer failed",
                    extra={
                        "event": "vector_memory_summarizer_failed",
                        "chat_id": chat_id,
                    },
                )
                summary = ""

        if summary:
            return summary

        # Fallback deterministic summary used when LLM summarization is unavailable.
        unique_lines: list[str] = []
        seen: set[str] = set()
        for raw in snippets:
            cleaned = " ".join(raw.split())
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique_lines.append(cleaned)
            if len(unique_lines) >= 12:
                break
        return "Consolidated memory summary:\n" + "\n".join(f"- {line}" for line in unique_lines)

    def _get_chat_rows(self, chat_id: str, max_items: int) -> list[dict[str, str]]:
        if not self.is_available:
            return []

        assert self._collection is not None
        try:
            result = self._collection.get(
                where={"chat_id": chat_id},
                include=["documents", "metadatas"],
                limit=max(1, max_items),
            )
        except Exception:
            logger.exception(
                "Failed to list chat rows for consolidation",
                extra={
                    "event": "vector_memory_get_rows_failed",
                    "chat_id": chat_id,
                },
            )
            return []

        ids = result.get("ids", [])
        docs = result.get("documents", [])
        metadatas = result.get("metadatas", [])
        if not isinstance(ids, list) or not isinstance(docs, list) or not isinstance(metadatas, list):
            return []

        rows: list[dict[str, str]] = []
        for doc_id, doc, metadata in zip(ids, docs, metadatas):
            if not isinstance(doc_id, str) or not isinstance(doc, str) or not isinstance(metadata, dict):
                continue
            rows.append(
                {
                    "id": doc_id,
                    "document": doc,
                    "source": str(metadata.get("source", "unknown")),
                    "timestamp_utc": str(metadata.get("timestamp_utc", "")),
                }
            )
        rows.sort(key=lambda r: r["timestamp_utc"])
        return rows
