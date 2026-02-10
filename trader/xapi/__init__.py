"""X API v2 integration.

Provides:
- rules management for the filtered stream
- stream consumption helpers with reconnect/backoff
- usage polling for conservative budget enforcement

All functionality is optional and gated by env/config.
"""
