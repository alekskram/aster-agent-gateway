"""aster_mcp - read-only, keyless MCP gateway to Aster DEX public data.

Package layout:
    rest.py    stdlib-only REST client for fapi.asterdex.com/fapi/v3
               and sapi.asterdex.com/api/v3 (weight-aware, TTL caches)
    chain.py   stdlib-only clients for tapi (Aster Chain JSON-RPC),
               Solana vault signatures and EVM vault Transfer logs
    server.py  FastMCP server wiring the 13 read-only tools

No API keys, no auth headers, no signing, no order placement - public
data only.
"""

__version__ = "0.1.0"
