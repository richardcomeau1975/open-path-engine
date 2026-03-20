"""Quick smoke test for file parser — run locally, not in CI."""

from app.services.file_parser import parse_text, parse_file, PARSERS

# Test 1: plain text parsing
sample = b"Hello world\nThis is a test."
result = parse_text(sample)
assert result == "Hello world\nThis is a test.", f"Got: {result}"
print("PASS: parse_text works")

# Test 2: extension mapping
assert ".pdf" in PARSERS
assert ".pptx" in PARSERS
assert ".docx" in PARSERS
assert ".xlsx" in PARSERS
assert ".txt" in PARSERS
assert ".md" in PARSERS
print("PASS: all 6 file types registered")

# Test 3: unsupported file type
try:
    parse_file("photo.jpg", b"fake")
    assert False, "Should have raised ValueError"
except ValueError:
    print("PASS: unsupported file type raises ValueError")

# Test 4: parse_file routes .txt correctly
result = parse_file("notes.txt", b"Some notes here")
assert result == "Some notes here", f"Got: {result}"
print("PASS: parse_file routes .txt correctly")

print("\nAll tests passed.")
