import os
import logging
from typing import List, Dict, Any, Optional
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

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
            # all-MiniLM-L6-v2 is fast and effective for this use case
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
            # Generate ID based on content hash or timestamp? 
            # Chroma needs unique IDs. Let's use timestamp + hash prefix
            import time
            import hashlib
            
            timestamp = time.time()
            text_hash = hashlib.md5(text.encode()).hexdigest()[:8]
            mem_id = f"mem_{int(timestamp)}_{text_hash}"
            
            embedding = self._get_embedding(text)
            
            # Default metadata
            meta = {
                "timestamp": timestamp,
                "date": time.strftime("%Y-%m-%d %H:%M:%S")
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
