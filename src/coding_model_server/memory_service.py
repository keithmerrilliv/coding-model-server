import hashlib
import os
import logging
import uuid
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

# chromadb and sentence-transformers (which drags in torch + transformers,
# ~6-15s and hundreds of MB RSS) are imported lazily inside _init_db
# (DEV-139): importing them at module scope meant server.py's top-level
# import paid the full cost BEFORE uvicorn could bind /health, extending
# every redeploy outage window — though nothing on the chat path needs
# these libraries until memory is actually used.
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from coding_model_server.code_chunker import CodeChunker
    _chunker = CodeChunker()
except ImportError:
    _chunker = None

logger = logging.getLogger(__name__)

# Suppress noisy pypdf warnings about malformed PDF objects
logging.getLogger("pypdf").setLevel(logging.ERROR)

if _chunker is None:
    logger.warning("tree_sitter_languages not available; language-aware chunking disabled")

MEMORY_RELEVANCE_THRESHOLD = float(os.getenv('MEMORY_RELEVANCE_THRESHOLD', '0.35'))
PDF_CHUNK_SIZE = int(os.getenv('PDF_CHUNK_SIZE', '1000'))
PDF_CHUNK_OVERLAP = int(os.getenv('PDF_CHUNK_OVERLAP', '200'))

# This file lives at src/coding_model_server/memory_service.py — two parents up reaches
# the repo root. Runtime state lives under var/ (see var/memory_db/). Resolve to
# an absolute path so the default works regardless of the caller's CWD.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MEMORY_DB = os.getenv("CODING_MODEL_MEMORY_DB", str(_REPO_ROOT / "var" / "memory_db"))


class MemoryService:
    def __init__(self, persist_directory: str = DEFAULT_MEMORY_DB):
        self.persist_directory = persist_directory
        self._collection = None
        self._embedding_model = None

        # Initialize automatically
        self._init_db()

    def _init_db(self, max_retries: int = 3):
        """Initialize ChromaDB and Embedding Model with retry for transient httpx errors."""
        # Deferred heavyweight imports — see the module docstring note
        # (DEV-139). First call pays the cost; the module import does not.
        import chromadb
        from sentence_transformers import SentenceTransformer

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Initializing Memory Service at {self.persist_directory}... (attempt {attempt}/{max_retries})")

                # Initialize Embedding Model (Force CPU to avoid RTX 5080 sm_120 compatibility issues)
                self._embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')

                # Initialize ChromaDB Client
                self.client = chromadb.PersistentClient(path=self.persist_directory)

                # Get or Create Collection
                self._collection = self.client.get_or_create_collection(
                    name="qwen_agent_memory",
                    metadata={"hnsw:space": "cosine"} # Cosine similarity for semantic search
                )

                # An empty collection is not an error — a fresh install has one — but it
                # is indistinguishable from a working one at query time: every search
                # simply finds nothing above the relevance threshold and the completion
                # proceeds without context. No error, no log line. The July 2026 move of
                # the default path to var/ left the populated DB behind at the repo root
                # and RAG silently retrieved nothing until someone counted the rows by
                # hand. So say it once, loudly, at boot.
                try:
                    count = self._collection.count()
                except Exception as e:  # best-effort; never fail startup over a count
                    logger.info("Memory Service initialized successfully (count unavailable: %s).", e)
                    return

                if count == 0:
                    logger.warning(
                        "Memory collection is EMPTY at %s — RAG retrieval will return nothing "
                        "for every query, and will do so SILENTLY. If you expected memories "
                        "here, check CODING_MODEL_MEMORY_DB: a populated database left at "
                        "another path is the usual cause.",
                        self.persist_directory,
                    )
                else:
                    logger.info(
                        "Memory Service initialized successfully (%s documents at %s).",
                        f"{count:,}", self.persist_directory,
                    )
                return

            except Exception as e:
                logger.error(f"Failed to initialize Memory Service (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(2)
                else:
                    raise

    def _get_embedding(self, text: str) -> List[float]:
        """Generate embedding for text using local model"""
        if not self._embedding_model:
            raise RuntimeError("Embedding model not initialized")
        return self._embedding_model.encode(text, show_progress_bar=False).tolist()

    def _add_batch(self, texts: List[str], metas: List[Dict[str, Any]]) -> int:
        """Embed, dedup, and insert many chunks with three calls, not 3×N.

        The per-chunk loop paid a separate CPU encode (per-call overhead
        dominates; batching is 10-30× faster), a separate ChromaDB dedup
        query, and a separate single-row add for every chunk — ~118
        sequential round-trips for a 200K-char ingest, all while the
        caller's request (and the agent turn behind it) waited (DEV-138).
        Returns the number of chunks actually inserted.
        """
        if not self._embedding_model:
            raise RuntimeError("Embedding model not initialized")

        hashes = [self._content_hash(t) for t in texts]
        try:
            existing = self._collection.get(
                where={"content_hash": {"$in": list(set(hashes))}})
            existing_hashes = {
                (m or {}).get("content_hash")
                for m in (existing.get("metadatas") or [])
            }
        except Exception as e:
            logger.warning("batch dedup lookup failed (%s) — inserting all", e)
            existing_hashes = set()

        timestamp = time.time()
        date_str = time.strftime("%Y-%m-%d %H:%M:%S")
        unique, seen = [], set()
        for text, meta, h in zip(texts, metas, hashes):
            if not text.strip() or h in existing_hashes or h in seen:
                continue
            seen.add(h)
            full_meta = {
                "timestamp": timestamp,
                "date": date_str,
                "source": "manual",
                "content_hash": h,
                **(meta or {}),
            }
            unique.append((text, full_meta))
        if not unique:
            return 0

        embeddings = self._embedding_model.encode(
            [t for t, _ in unique], show_progress_bar=False)
        ids = [f"mem_{int(timestamp)}_{uuid.uuid4().hex[:12]}" for _ in unique]
        self._collection.add(
            documents=[t for t, _ in unique],
            embeddings=[e.tolist() for e in embeddings],
            metadatas=[m for _, m in unique],
            ids=ids,
        )
        logger.info("Batch memory insert: %d chunks (%d duplicates skipped)",
                    len(unique), len(texts) - len(unique))
        return len(unique)

    def _content_hash(self, text: str) -> str:
        """SHA-256 hex digest of text, used for deduplication.

        SHA-256 (not MD5): not security-critical here, but using a
        cryptographic-looking hash invites future misuse and MD5 is
        widely flagged by static analyzers. Truncate to 32 hex chars
        (128 bits) to match the previous storage size for any existing
        ChromaDB rows that already carry a content_hash field.
        """
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    def add_memory(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Add a new memory string to the database. Returns dict with status on success, error on failure."""
        if not text or not text.strip():
            return {"error": "Text cannot be empty"}

        try:
            # Dedup check: skip if identical content already stored
            content_hash = self._content_hash(text)
            existing = self._collection.get(where={"content_hash": content_hash}, limit=1)
            if existing and existing["ids"]:
                logger.info("Duplicate memory skipped (hash %s)", content_hash[:8])
                return {"status": "duplicate", "id": existing["ids"][0]}

            timestamp = time.time()
            mem_id = f"mem_{int(timestamp)}_{uuid.uuid4().hex[:12]}"

            embedding = self._get_embedding(text)
            
            # Default metadata
            meta = {
                "timestamp": timestamp,
                "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "manual",
                "content_hash": content_hash,
            }
            if metadata:
                meta.update(metadata)
                
            self._collection.add(
                documents=[text],
                embeddings=[embedding],
                metadatas=[meta],
                ids=[mem_id]
            )
            
            logger.info(f"Memory saved: {mem_id}")
            return {"status": "success", "id": mem_id}
            
        except Exception as e:
            logger.error(f"Error adding memory: {e}")
            return {"error": str(e)}

    def add_memory_chunked(self, text: str, source: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Add text to the database using language-aware chunking when possible.

        If *source* has a recognized code extension and tree-sitter is available,
        the text is parsed into AST-aware chunks. Otherwise a simple sliding-window
        chunker is used (2000 chars, 300 overlap).

        Returns dict with status and chunks_added count.
        """
        if not text or not text.strip():
            return {"error": "Text cannot be empty"}

        ext = os.path.splitext(source)[1].lower() if source else ""
        # Fall back to bare filename for Makefile, Dockerfile, etc.
        if not ext and source:
            ext = os.path.basename(source)

        # Try tree-sitter chunking
        if _chunker and ext and _chunker.get_lang_name(ext):
            chunks = _chunker.chunk_text(text, ext)
        elif _chunker:
            chunks = _chunker.simple_chunk(text, source or "<unknown>", 2000)
        else:
            # Fallback: simple sliding-window (no tree-sitter available)
            chunks = self._simple_chunk(text, source or "<unknown>", 2000, 300)

        if not chunks:
            # Single chunk fallback
            return self.add_memory(text, metadata={"source": source or "manual", **(metadata or {})})

        texts, metas = [], []
        for i, chunk in enumerate(chunks):
            chunk_meta = {
                "source": chunk.get("metadata", {}).get("source", source or "manual"),
                "node_type": chunk.get("metadata", {}).get("type", "plain_text"),
                "context": chunk.get("metadata", {}).get("context", ""),
                "chunk_index": i,
            }
            if metadata:
                chunk_meta.update(metadata)
            texts.append(chunk["text"])
            metas.append(chunk_meta)

        try:
            added = self._add_batch(texts, metas)
        except Exception as e:
            logger.error("Batched ingest failed: %s", e)
            return {"error": str(e)}
        return {"status": "success", "chunks_added": added}

    @staticmethod
    def _simple_chunk(text: str, source: str, max_chars: int = 2000, overlap: int = 300) -> List[Dict]:
        """Fallback sliding-window chunker for when tree-sitter is unavailable.

        Mirrors CodeChunker.simple_chunk() from code_chunker.py so that chunk
        boundaries stay consistent regardless of which path is taken.
        """
        chunks = []
        start = 0
        while start < len(text):
            end = start + max_chars
            chunks.append({
                "text": text[start:end],
                "metadata": {"source": source, "type": "plain_text"}
            })
            if end >= len(text):
                break
            start += (max_chars - overlap)
        return chunks

    def ingest_pdf(self, file_path: str) -> Dict[str, Any]:
        """Read a PDF, chunk it, and add to vector store"""
        if not PdfReader:
            return {"error": "pypdf library not installed"}

        if not os.path.exists(file_path):
            return {"error": f"File not found: {file_path}"}

        try:
            reader = PdfReader(file_path)
            filename = os.path.basename(file_path)
            total_pages = len(reader.pages)

            logger.info(f"Ingesting PDF: {filename} ({total_pages} pages)")

            full_text = ""
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    full_text += f"\n--- Page {i+1} ---\n{page_text}"

            if not full_text.strip():
                return {"error": "No extractable text found in PDF"}

            chunks = self._simple_chunk(full_text, filename, PDF_CHUNK_SIZE, PDF_CHUNK_OVERLAP)
            chunks_added = self._add_batch(
                [c["text"] for c in chunks],
                [{"source": filename, "type": "pdf", "chunk_index": i}
                 for i in range(len(chunks))],
            )

            return {
                "status": "success",
                "filename": filename,
                "pages": total_pages,
                "chunks": chunks_added
            }

        except Exception as e:
            logger.error(f"Failed to ingest PDF {file_path}: {e}")
            return {"error": str(e)}

    def search_memory(self, query: str, n_results: int = 3) -> List[Dict[str, Any]]:
        """Search for relevant memories, filtered by cosine distance threshold.

        Returns a list of dicts with 'document' and 'distance' keys.
        """
        if not query or not query.strip():
            return []

        try:
            query_embedding = self._get_embedding(query)

            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "distances"]
            )

            documents = results['documents'][0] if results['documents'] else []
            distances = results['distances'][0] if results.get('distances') else []

            filtered = []
            for doc, dist in zip(documents, distances):
                if dist <= MEMORY_RELEVANCE_THRESHOLD:
                    filtered.append({"document": doc, "distance": dist})

            return filtered

        except Exception as e:
            logger.error(f"Error searching memory: {e}")
            return []

    def get_context_string(self, query: str, max_tokens: int = 1000) -> str:
        """Get a formatted string of relevant memories for prompt injection.

        Truncates output to approximately max_tokens (estimated at 4 chars/token).
        """
        memories = self.search_memory(query, n_results=5)

        if not memories:
            return ""

        char_budget = max_tokens * 4
        header = "## RELEVANT MEMORIES (FACTS & DECISIONS):\n"
        context_str = header
        for i, mem in enumerate(memories, 1):
            entry = f"{i}. {mem['document']}\n"
            if len(context_str) + len(entry) > char_budget:
                break
            context_str += entry

        if context_str == header:
            return ""

        return context_str + "\n"