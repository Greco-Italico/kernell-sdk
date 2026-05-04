"""
Kernell OS SDK — Token Estimator
═════════════════════════════════
Local token estimation without API calls.
Uses bytes-per-token heuristics with a 33% safety factor.
Migrated from Kernell OS core/token_estimator.py.
"""

BYTES_PER_TOKEN = {
    "text": 4, "py": 4, "js": 4, "ts": 4, "md": 4, "sh": 4,
    "yaml": 3, "yml": 3, "toml": 3, "csv": 3,
    "json": 2, "xml": 2, "html": 2,
}
SAFETY_FACTOR = 4 / 3

def estimate_tokens(content: str, file_type: str = "text") -> int:
    """Estimate tokens for a string. Conservative (33% pad)."""
    bpt = BYTES_PER_TOKEN.get(file_type, 4)
    raw = len(content.encode("utf-8", errors="replace")) / bpt
    return int(raw * SAFETY_FACTOR)

def estimate_messages_tokens(messages: list) -> int:
    """Estimate total tokens for a list of message dicts."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += estimate_tokens(str(block.get("text", "")))
        total += 4  # role/name overhead per message
    return total
