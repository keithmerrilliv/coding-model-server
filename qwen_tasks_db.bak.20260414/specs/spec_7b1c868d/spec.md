# Word frequency counter

## Goal
Build a Python 3.11 CLI that reads a text file and prints the top N
most frequent words, one per line, with their counts.

## Commands
- `wordfreq <file> [--top N]` — print the top N words (default 10)
- Words are case-insensitive, punctuation stripped
- Output format: `<count> <word>` sorted by count descending

## Acceptance criteria
- Reads from a file path given as the first argument
- `--top N` flag works (default 10)
- Case-insensitive (Hello == hello)
- Strips common punctuation (.,;:!?"'()-—)
- Exits with code 1 and a clear message if the file doesn't exist
- Handles empty files gracefully (prints nothing, exits 0)

## Constraints
- Python 3.11+ standard library only (no pip packages)
- Single file: `wordfreq.py`

## Test strategy
- Framework: pytest
- Required: yes
- Test file: `test_wordfreq.py`

## Output location
- New project (workspace-local)
