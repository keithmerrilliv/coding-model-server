import os
import logging

# tree_sitter_languages is unmaintained and publishes no wheel for Python 3.13+
# (DEV-441), so on a newer interpreter it cannot be installed at all. Import it
# softly, the way memory_service.py already treats this same package: a missing
# backend disables language-aware chunking and every caller falls back to
# simple_chunk(), instead of making this module unimportable and taking the
# memory service down with it.
try:
    from tree_sitter_languages import get_parser
except ImportError:
    get_parser = None

logger = logging.getLogger(__name__)

if get_parser is None:
    logger.warning("tree_sitter_languages not available; language-aware chunking disabled")

class CodeChunker:
    def __init__(self):
        self.parsers = {}
        self._failed_langs = set()  # languages that failed to load — skip silently
        # Mapping extension to tree-sitter language name.
        # NOTE: 'swift' is NOT in tree_sitter_languages — it falls back to
        # simple chunking gracefully via get_parser_for_ext's try/except.
        self.extension_map = {
            # Apple / systems
            '.swift': 'swift',          # not in tree_sitter_languages — graceful fallback
            '.metal': 'cpp',            # Metal ≈ C++ for basic chunking
            '.h': 'cpp',
            '.cpp': 'cpp',
            '.cc': 'cpp',
            '.cxx': 'cpp',
            '.c': 'c',
            '.m': 'objc',
            '.mm': 'cpp',
            # Python
            '.py': 'python',
            '.pyi': 'python',
            # JavaScript / TypeScript
            '.js': 'javascript',
            '.jsx': 'javascript',
            '.mjs': 'javascript',
            '.ts': 'typescript',
            '.tsx': 'tsx',
            # Shell
            '.sh': 'bash',
            '.bash': 'bash',
            '.zsh': 'bash',
            # Data / config
            '.json': 'json',
            '.yaml': 'yaml',
            '.yml': 'yaml',
            '.toml': 'toml',
            # Web
            '.html': 'html',
            '.htm': 'html',
            '.css': 'css',
            # Documentation
            '.md': 'markdown',
            '.markdown': 'markdown',
            # Other languages likely to encounter
            '.rb': 'ruby',
            '.pl': 'perl',
            '.pm': 'perl',
            '.go': 'go',
            '.rs': 'rust',
            '.java': 'java',
            '.kt': 'kotlin',
            '.cs': 'c_sharp',
            '.sql': 'sql',
            '.lua': 'lua',
            '.r': 'r',
            '.R': 'r',
            '.scala': 'scala',
            '.php': 'php',
            # Build / infra
            '.mk': 'make',
            '.dockerfile': 'dockerfile',
        }

        # Bare filenames that map to a language (no extension)
        self.filename_map = {
            'Makefile': 'make',
            'GNUmakefile': 'make',
            'Dockerfile': 'dockerfile',
        }

        # Node types that we want to treat as a "chunk"
        self.chunk_types = {
            'swift': {'class_declaration', 'extension_declaration', 'function_declaration',
                      'struct_declaration', 'enum_declaration', 'protocol_declaration'},
            'cpp': {'class_specifier', 'function_definition', 'struct_specifier',
                    'namespace_definition', 'template_declaration'},
            'c': {'function_definition', 'struct_specifier', 'enum_specifier'},
            'python': {'class_definition', 'function_definition'},
            'objc': {'interface_declaration', 'implementation_declaration', 'function_definition'},
            'javascript': {'class_declaration', 'function_declaration', 'function',
                           'arrow_function', 'method_definition', 'export_statement'},
            'typescript': {'class_declaration', 'function_declaration', 'function',
                           'arrow_function', 'method_definition', 'export_statement',
                           'interface_declaration', 'type_alias_declaration'},
            'tsx': {'class_declaration', 'function_declaration', 'function',
                    'arrow_function', 'method_definition', 'export_statement',
                    'interface_declaration', 'type_alias_declaration'},
            'bash': {'function_definition', 'compound_statement'},
            'json': {'pair'},  # top-level keys
            'yaml': {'block_mapping_pair'},
            'html': {'element'},
            'markdown': {'section', 'atx_heading'},
            'ruby': {'class', 'method', 'module', 'singleton_method'},
            'go': {'function_declaration', 'method_declaration', 'type_declaration'},
            'rust': {'function_item', 'impl_item', 'struct_item', 'enum_item',
                     'trait_item', 'mod_item'},
            'java': {'class_declaration', 'method_declaration', 'interface_declaration',
                     'enum_declaration'},
            'kotlin': {'class_declaration', 'function_declaration', 'object_declaration'},
            'c_sharp': {'class_declaration', 'method_declaration', 'interface_declaration',
                        'struct_declaration', 'enum_declaration'},
            'sql': {'create_table_statement', 'select_statement', 'insert_statement'},
            'lua': {'function_declaration', 'function_definition'},
            'scala': {'class_definition', 'function_definition', 'object_definition',
                      'trait_definition'},
            'php': {'class_declaration', 'function_definition', 'method_declaration'},
            'perl': {'subroutine_declaration_statement'},
            'make': {'rule'},
        }

    def get_lang_name(self, ext_or_filename):
        """Resolve an extension ('.py') or bare filename ('Makefile') to a tree-sitter language name."""
        return self.extension_map.get(ext_or_filename) or self.filename_map.get(ext_or_filename)

    def get_parser_for_ext(self, ext):
        # No backend at all — every extension falls back to simple chunking.
        if get_parser is None:
            return None
        lang_name = self.get_lang_name(ext)
        if not lang_name:
            return None
        if lang_name in self._failed_langs:
            return None

        if lang_name not in self.parsers:
            try:
                self.parsers[lang_name] = get_parser(lang_name)
            except Exception as e:
                logger.warning(f"Failed to load parser for {lang_name}: {e}")
                self._failed_langs.add(lang_name)
                return None
        return self.parsers[lang_name]

    def chunk_file(self, file_path, max_chars=3000):
        ext = os.path.splitext(file_path)[1].lower()
        # Fall back to bare filename for files like Makefile, Dockerfile
        if not ext:
            ext = os.path.basename(file_path)
        parser = self.get_parser_for_ext(ext)

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"Failed to read {file_path}: {e}")
            return []

        if not parser:
            return self.simple_chunk(content, file_path)

        lang_name = self.get_lang_name(ext)
        # Parse and slice the SAME bytes object (DEV-161): node offsets are
        # byte offsets, so indexing the str with them shifted every chunk
        # boundary after the first multibyte character.
        content_b = content.encode('utf-8')
        try:
            tree = parser.parse(content_b)
        except Exception as e:
            logger.warning(f"tree-sitter parse failed for {file_path}: {e}")
            return self.simple_chunk(content, file_path)
        root_node = tree.root_node

        chunks = []
        self._recursive_chunk(root_node, content_b, file_path, lang_name, chunks, max_chars)

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

        lang_name = self.get_lang_name(ext)
        # See chunk_file: slice the same bytes the parser saw (DEV-161).
        text_b = text.encode('utf-8')
        try:
            tree = parser.parse(text_b)
        except Exception as e:
            logger.warning(f"tree-sitter parse failed for {source}: {e}")
            return self.simple_chunk(text, source, max_chars)
        root_node = tree.root_node

        chunks = []
        self._recursive_chunk(root_node, text_b, source, lang_name, chunks, max_chars)

        if not chunks:
            return self.simple_chunk(text, source, max_chars)

        return chunks

    def _recursive_chunk(self, node, content_b, file_path, lang_name, chunks, max_chars, context=""):
        """Walk the parse tree, emitting chunks. ``content_b`` is the utf-8
        BYTES the parser was given — node.start_byte/end_byte index into it
        (DEV-161). Each slice is decoded back to str for storage."""
        node_text = content_b[node.start_byte:node.end_byte].decode('utf-8', 'replace')
        
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
                        self._recursive_chunk(child, content_b, file_path, lang_name, chunks, max_chars, new_context)
                        has_subchunks = True
                
                if not has_subchunks:
                    # Fallback if no meaningful children found
                    self._add_split_chunks(node_text, file_path, node.type, context, chunks, max_chars)
                return

        # If it's the root or we haven't found a chunk type yet, keep digging
        chunks_before = len(chunks)
        for child in node.children:
            if child.end_byte - child.start_byte > 200:
                self._recursive_chunk(child, content_b, file_path, lang_name, chunks, max_chars, context)

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
