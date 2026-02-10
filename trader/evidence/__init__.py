"""Evidence acquisition layer.

Implements the "LLM scout" -> "explicit acquisition" pattern:
- LLM search results produce candidate URLs
- We fetch selected URLs ourselves (bounded)
- Extract main article text + metadata
- Persist as immutable evidence docs referenced from Snapshots
"""
