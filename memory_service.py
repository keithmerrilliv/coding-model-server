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

logger = logging.getLogger(__name__)

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
            
            # Initialize Embedding Model (CPU-based, lightweight)
            self._embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            
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

    def add_memory(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Add a new memory string to the database"""
        if not text or not text.strip():
            return "Empty text ignored"
            
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
            return mem_id
            
        except Exception as e:
            logger.error(f"Error adding memory: {e}")
            return None

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
            
            # Chunking logic
            chunk_size = 1000
            overlap = 200
            
            start = 0
            while start < len(full_text):
                end = start + chunk_size
                chunk = full_text[start:end]
                
                # Add metadata for the chunk
                meta = {
                    "source": filename,
                    "type": "pdf",
                    "chunk_index": chunks_added
                }
                
                self.add_memory(chunk, metadata=meta)
                chunks_added += 1
                
                start += (chunk_size - overlap)
                
            return {
                "status": "success",
                "filename": filename,
                "pages": total_pages,
                "chunks": chunks_added
            }
            
        except Exception as e:
            logger.error(f"Failed to ingest PDF {file_path}: {e}")
            return {"error": str(e)}

    def search_memory(self, query: str, n_results: int = 3) -> List[str]:
        """Search for relevant memories"""
        if not query or not query.strip():
            return []
            
        try:
            query_embedding = self._get_embedding(query)
            
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results
            )
            
            # Flatten results (Chroma returns list of lists)
            documents = results['documents'][0] if results['documents'] else []
            return documents
            
        except Exception as e:
            logger.error(f"Error searching memory: {e}")
            return []

    def get_context_string(self, query: str, max_tokens: int = 1000) -> str:
        """Get a formatted string of relevant memories for prompt injection"""
        memories = self.search_memory(query, n_results=5)
        
        if not memories:
            return ""
            
        context_str = "## RELEVANT MEMORIES (FACTS & DECISIONS):\n"
        for i, mem in enumerate(memories, 1):
            context_str += f"{i}. {mem}\n"
            
        return context_str + "\n"
