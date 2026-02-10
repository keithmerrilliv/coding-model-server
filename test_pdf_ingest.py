import logging
import warnings
from memory_service import MemoryService
import os

# Configure logging to see everything
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('test_pdf')

# Capture warnings
def warn_with_log(message, category, filename, lineno, file=None, line=None):
    logger.warning(f"Warning: {category.__name__}: {message} ({filename}:{lineno})")

warnings.showwarning = warn_with_log

ms = MemoryService()
pdf_path = os.path.abspath("docs/deepseek_vs_qwen_comparison.pdf")
print(f"Testing ingestion of {pdf_path}")
result = ms.ingest_pdf(pdf_path)
print(f"Result: {result}")
