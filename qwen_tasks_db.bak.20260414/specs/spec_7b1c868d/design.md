# Architecture: Word Frequency Counter CLI

## Overview
A single-file Python CLI tool that analyzes text files and reports the most frequent words. Uses argparse for command-line parsing, regex for word extraction, and collections.Counter for frequency counting. Standard library only.

## Components
- **wordfreq.py**: Main CLI application with argument parsing, file reading, word tokenization, frequency analysis, and output formatting
- **test_wordfreq.py**: Pytest test suite covering all acceptance criteria including edge cases

## File Structure
```
/
├── wordfreq.py          # Main CLI application
└── test_wordfreq.py     # pytest test suite
```

## Data Models
No custom data models. Use:
- `argparse.Namespace` for parsed arguments (file, top)
- `collections.Counter[str]` for word frequency mapping
- Output format: `<int> <str>` per line

## Implementation Notes
1. **Word extraction**: Use regex `[a-zA-Z]+` after lowercasing to extract words and strip punctuation in one pass
2. **Case handling**: Convert entire file content to lowercase before tokenization
3. **Punctuation stripping**: Regex approach handles all specified chars (.,;:!?"'()-—) automatically by only matching letters
4. **Error handling**: Check file existence with `os.path.exists()` or catch `FileNotFoundError`; exit code 1 with stderr message for missing files
5. **Empty file**: Counter will be empty, loop produces no output, exits 0 naturally
6. **Sorting**: Use `most_common(top)` from Counter which handles ties consistently

## Acceptance Criteria Checklist
- [ ] Reads from a file path given as the first argument
- [ ] `--top N` flag works (default 10)
- [ ] Case-insensitive (Hello == hello)
- [ ] Strips common punctuation (.,;:!?"'()-—)
- [ ] Exits with code 1 and a clear message if the file doesn't exist
- [ ] Handles empty files gracefully (prints nothing, exits 0)