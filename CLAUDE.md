# Project Guidelines

## Documentation Lookup

When working with external libraries, frameworks, or APIs — **always use the Context7 MCP server** (`resolve-library-id` then `query-docs`) to look up current documentation and code examples before writing code or giving advice. Do not rely on training data for API signatures, parameter names, or usage patterns — they may be outdated or wrong.

Specifically:
- **Before using any library API** you haven't already verified in this session, look it up via Context7.
- **When the user asks about a framework** (features, limitations, how something works), query Context7 for authoritative answers rather than guessing.
- **When writing code that uses an external library**, verify the import paths, method signatures, and parameter names against Context7 docs.
- If Context7 doesn't have the library, fall back to `WebSearch` or `WebFetch` — but prefer Context7 first since it returns structured, verified documentation.
