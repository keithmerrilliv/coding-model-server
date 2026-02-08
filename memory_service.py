import os
import logging
import hashlib
import time
from typing import List, Dict, Any, Optional
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from code_chunker import CodeChunker
    _chunker = CodeChunker()
except ImportError:
    _chunker = None

logger = logging.getLogger(__name__)

if _chunker is None:
    logger.warning("tree_sitter_languages not available; language-aware chunking disabled")

MEMORY_RELEVANCE_THRESHOLD = float(os.getenv('MEMORY_RELEVANCE_THRESHOLD', '0.35'))
PDF_CHUNK_SIZE = int(os.getenv('PDF_CHUNK_SIZE', '1000'))
PDF_CHUNK_OVERLAP = int(os.getenv('PDF_CHUNK_OVERLAP', '200'))


class MemoryService:
    def __init__(self, persist_directory: str = "qwen_memory_db"):
        self.persist_directory = persist_directory
        self._collection = None
        self._embedding_model = None

        # Initialize automatically
        self._init_db()

    def _init_db(self):
        """Initialize ChromaDB and Embedding Model"""
        try:
            logger.info(f"Initializing Memory Service at {self.persist_directory}...")
            
            # Initialize Embedding Model (Force CPU to avoid RTX 5080 sm_120 compatibility issues)
            self._embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
            
            # Initialize ChromaDB Client
            self.client = chromadb.PersistentClient(path=self.persist_directory)
            
            # Get or Create Collection
            self._collection = self.client.get_or_create_collection(
                name="qwen_agent_memory",
                metadata={"hnsw:space": "cosine"} # Cosine similarity for semantic search
            )
            
            logger.info("Memory Service initialized successfully.")
            
        except Exception as e:
            logger.error(f"Failed to initialize Memory Service: {e}")
            raise

    def _get_embedding(self, text: str) -> List[float]:
        """Generate embedding for text using local model"""
        if not self._embedding_model:
            raise RuntimeError("Embedding model not initialized")
        return self._embedding_model.encode(text).tolist()

    def add_memory(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Add a new memory string to the database. Returns dict with status on success, error on failure."""
        if not text or not text.strip():
            return {"error": "Text cannot be empty"}
            
        try:
            timestamp = time.time()
            text_hash = hashlib.md5(text.encode()).hexdigest()[:8]
            mem_id = f"mem_{int(timestamp)}_{text_hash}"
            
            embedding = self._get_embedding(text)
            
            # Default metadata
            meta = {
                "timestamp": timestamp,
                "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "manual"
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
        chunker is used (2000 chars, 200 overlap).

        Returns dict with status and chunks_added count.
        """
        if not text or not text.strip():
            return {"error": "Text cannot be empty"}

        ext = os.path.splitext(source)[1].lower() if source else ""

        # Try tree-sitter chunking
        if _chunker and ext and ext in _chunker.extension_map:
            chunks = _chunker.chunk_text(text, ext)
        elif _chunker:
            chunks = _chunker.simple_chunk(text, source or "<unknown>", 2000)
        else:
            # Fallback: simple sliding-window (no tree-sitter available)
            chunks = self._simple_chunk(text, source or "<unknown>", 2000, 200)

        if not chunks:
            # Single chunk fallback
            return self.add_memory(text, metadata={"source": source or "manual", **(metadata or {})})

        added = 0
        for i, chunk in enumerate(chunks):
            chunk_meta = {
                "source": chunk.get("metadata", {}).get("source", source or "manual"),
                "node_type": chunk.get("metadata", {}).get("type", "plain_text"),
                "context": chunk.get("metadata", {}).get("context", ""),
                "chunk_index": i,
            }
            if metadata:
                chunk_meta.update(metadata)

            result = self.add_memory(chunk["text"], metadata=chunk_meta)
            if "error" not in result:
                added += 1

        return {"status": "success", "chunks_added": added}

    @staticmethod
    def _simple_chunk(text: str, source: str, max_chars: int = 2000, overlap: int = 200) -> List[Dict]:
        """Basic sliding-window chunker used when tree-sitter is unavailable."""
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
            chunks_added = 0
            
            logger.info(f"Ingesting PDF: {filename} ({total_pages} pages)")
            
            # Simple chunking: Page by page, or text sliding window
            # Let's do a sliding window of 1000 chars with 200 overlap
            full_text = ""
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    full_text += f"\n--- Page {i+1} ---\n{page_text}"
            
            if not full_text.strip():
                return {"error": "No extractable text found in PDF"}

            # Chunking logic
            start = 0
            while start < len(full_text):
                end = start + PDF_CHUNK_SIZE
                chunk = full_text[start:end]
                
                # Add metadata for the chunk
                meta = {
                    "source": filename,
                    "type": "pdf",
                    "chunk_index": chunks_added
                }
                
                self.add_memory(chunk, metadata=meta)
                chunks_added += 1
                
                start += (PDF_CHUNK_SIZE - PDF_CHUNK_OVERLAP)
                
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

    def reset_database(self) -> Dict[str, Any]:
        """Wipe the entire collection and re-initialize."""
        try:
            logger.info("Resetting RAG database...")
            self.client.delete_collection("qwen_agent_memory")
            self._collection = self.client.create_collection(
                name="qwen_agent_memory",
                metadata={"hnsw:space": "cosine"}
            )
            logger.info("Database reset successful.")
            return {"status": "success", "message": "Database wiped and re-initialized."}
        except Exception as e:
            logger.error(f"Failed to reset database: {e}")
            return {"error": str(e)}

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

    def dispose(self):
        """Dispose of the embedding model to free memory"""
        if self._embedding_model:
            del self._embedding_model
            self._embedding_model = None
            import gc
            gc.collect()
        logger.info("Memory service resources disposed.")