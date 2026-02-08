import os
import logging
from tree_sitter_languages import get_language, get_parser

logger = logging.getLogger(__name__)

class CodeChunker:
    def __init__(self):
        self.parsers = {}
        # Mapping extension to tree-sitter language name
        self.extension_map = {
            '.swift': 'swift',
            '.metal': 'cpp',  # Metal is close enough to C++ for basic chunking
            '.h': 'cpp',
            '.cpp': 'cpp',
            '.c': 'c',
            '.m': 'objc',
            '.mm': 'cpp',
            '.py': 'python',
            '.md': 'markdown'
        }
        
        # Node types that we want to treat as a "chunk"
        self.chunk_types = {
            'swift': {'class_declaration', 'extension_declaration', 'function_declaration', 'struct_declaration', 'enum_declaration', 'protocol_declaration'},
            'cpp': {'class_specifier', 'function_definition', 'struct_specifier', 'namespace_definition'},
            'c': {'function_definition', 'struct_specifier', 'enum_specifier'},
            'python': {'class_definition', 'function_definition'},
            'objc': {'interface_declaration', 'implementation_declaration', 'function_definition'},
            'markdown': {'section', 'atx_heading'},
        }

    def get_parser_for_ext(self, ext):
        lang_name = self.extension_map.get(ext)
        if not lang_name:
            return None
        
        if lang_name not in self.parsers:
            try:
                self.parsers[lang_name] = get_parser(lang_name)
            except Exception as e:
                logger.warning(f"Failed to load parser for {lang_name}: {e}")
                return None
        return self.parsers[lang_name]

    def chunk_file(self, file_path, max_chars=3000):
        ext = os.path.splitext(file_path)[1].lower()
        parser = self.get_parser_for_ext(ext)

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"Failed to read {file_path}: {e}")
            return []

        if not parser:
            return self.simple_chunk(content, file_path)

        lang_name = self.extension_map.get(ext)
        tree = parser.parse(bytes(content, 'utf-8'))
        root_node = tree.root_node

        chunks = []
        self._recursive_chunk(root_node, content, file_path, lang_name, chunks, max_chars)

        # If no chunks were extracted (e.g. no recognized node types), do simple chunk
        if not chunks:
            return self.simple_chunk(content, file_path)

        return chunks

    def chunk_text(self, text, ext, max_chars=3000):
        """Chunk raw text content given a file extension hint (e.g. '.py', '.swift').

        Works like chunk_file() but operates on text directly instead of reading
        from disk. Useful when the server receives code as a string.
        """
        if not ext.startswith('.'):
            ext = '.' + ext
        ext = ext.lower()

        source = f"<text>{ext}"
        parser = self.get_parser_for_ext(ext)

        if not parser:
            return self.simple_chunk(text, source, max_chars)

        lang_name = self.extension_map.get(ext)
        tree = parser.parse(bytes(text, 'utf-8'))
        root_node = tree.root_node

        chunks = []
        self._recursive_chunk(root_node, text, source, lang_name, chunks, max_chars)

        if not chunks:
            return self.simple_chunk(text, source, max_chars)

        return chunks

    def _recursive_chunk(self, node, content, file_path, lang_name, chunks, max_chars, context=""):
        node_text = content[node.start_byte:node.end_byte]
        
        # If this node is a chunk type and its size is reasonable
        if node.type in self.chunk_types.get(lang_name, set()):
            if len(node_text) <= max_chars:
                chunks.append({
                    "text": node_text,
                    "metadata": {
                        "source": file_path,
                        "type": node.type,
                        "context": context
                    }
                })
                return
            else:
                # Node is too big, try to chunk its children
                new_context = f"{context} > {node.type}"
                has_subchunks = False
                for child in node.children:
                    if child.end_byte - child.start_byte > 100: # Ignore tiny children
                        self._recursive_chunk(child, content, file_path, lang_name, chunks, max_chars, new_context)
                        has_subchunks = True
                
                if not has_subchunks:
                    # Fallback if no meaningful children found
                    self._add_split_chunks(node_text, file_path, node.type, context, chunks, max_chars)
                return

        # If it's the root or we haven't found a chunk type yet, keep digging
        chunks_before = len(chunks)
        for child in node.children:
            if child.end_byte - child.start_byte > 200:
                self._recursive_chunk(child, content, file_path, lang_name, chunks, max_chars, context)

        # If no children produced chunks, but the node itself has text
        if len(chunks) == chunks_before and node.type != 'module' and len(node_text.strip()) > 100:
             self._add_split_chunks(node_text, file_path, node.type, context, chunks, max_chars)

    def _add_split_chunks(self, text, file_path, node_type, context, chunks, max_chars):
        # Split large text into overlapping chunks
        overlap = 200
        start = 0
        while start < len(text):
            end = start + max_chars
            chunk = text[start:end]
            chunks.append({
                "text": chunk,
                "metadata": {
                    "source": file_path,
                    "type": f"{node_type}_split",
                    "context": context
                }
            })
            if end >= len(text): break
            start += (max_chars - overlap)

    def simple_chunk(self, content, file_path, max_chars=3000):
        overlap = 300
        start = 0
        chunks = []
        while start < len(content):
            end = start + max_chars
            chunk = content[start:end]
            chunks.append({
                "text": chunk,
                "metadata": {
                    "source": file_path,
                    "type": "plain_text"
                }
            })
            if end >= len(content): break
            start += (max_chars - overlap)
        return chunks
