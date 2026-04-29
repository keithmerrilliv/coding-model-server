#!/usr/bin/env python3
"""Word frequency counter CLI tool."""

import argparse
import os
import re
import sys
from collections import Counter


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Count word frequencies in a text file"
    )
    parser.add_argument("file", help="Path to the text file")
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Number of top words to display (default: 10)"
    )
    return parser.parse_args()


def read_file(filepath):
    """Read and return file contents."""
    if not os.path.exists(filepath):
        print(f"Error: File '{filepath}' does not exist", file=sys.stderr)
        sys.exit(1)
    
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def extract_words(text):
    """Extract words from text, handling Unicode and stripping punctuation."""
    # Convert to lowercase for case-insensitivity
    text = text.lower()
    # Match Unicode letters (including accented characters) and numbers
    # This handles Unicode words properly
    words = re.findall(r"\w+", text, re.UNICODE)
    return words


def count_frequencies(words):
    """Count word frequencies using Counter."""
    return Counter(words)


def get_top_words(counter, n):
    """Get the top N most common words."""
    if n <= 0:
        return []
    return counter.most_common(n)


def format_output(top_words):
    """Format output as '<count> <word>' per line."""
    for word, count in top_words:
        print(f"{count} {word}")


def main():
    """Main entry point."""
    args = parse_args()
    
    # Validate --top value
    if args.top < 0:
        print("Error: --top must be non-negative", file=sys.stderr)
        sys.exit(1)
    
    # Read file
    text = read_file(args.file)
    
    # Extract and count words
    words = extract_words(text)
    counter = count_frequencies(words)
    
    # Get top words and output
    top_words = get_top_words(counter, args.top)
    format_output(top_words)


if __name__ == "__main__":
    main()