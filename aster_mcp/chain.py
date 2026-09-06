"""Stdlib-only clients for Aster on-chain surfaces: tapi (Aster Chain
JSON-RPC), Solana vault signatures, and EVM vault Transfer logs.

tapi  POST https://tapi.asterdex.com/info  {"jsonrpc":"2.0","id":1,
      "method":"aster_getBalance","params":{...}} - answers keyless,
      but account privacy hides most accounts: private accounts return
      an EMPTY result (or accountPrivacy:"enabled") - that is an honest
      degradation, never an error-dict pretending data exists. Methods
      used (read-only): aster_getBalance / aster_openOrders /
      aster_userFills (+ spot variants when they exist).

solana RPC https://api.mainnet-beta.solana.com getSignaturesForAddress
      for the Aster vault program
      EhUtRgu9iEbZXXRpEvDj6n1wnQRjMi2SERDo3c6bmN2c (keyless, fresh
      signatures: signature/slot/blockTime/err).

evm   eth_getLogs Transfer(topic0=0xddf252ad...) to the Aster vault
      contracts on BSC/ETH/ARB. Free public RPCs reject even 300-block
      windows with -32005 (limit exceeded), so the RPC URL comes from
      env ASTER_EVM_RPC_URL / ASTER_EVM_RPC_URL_{BSC,ETH,ARB}; when
      unset the tools return an honest "no RPC configured" error dict -
      NEVER an invented log, NEVER a traceback.

Each surface keeps its own small rate ledger (solana 10 req/min is
polite for a free public node; tapi 30/min) and raises RpcError with a
taxonomy kind so tools can degrade honestly.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

TAPI_URL = "https://tapi.asterdex.com/info"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
ASTER_SOLANA_VAULT = "EhUtRgu9iEbZXXRpEvDj6n1wnQRjMi2SERDo3c6bmN2c"

# EVM vaults (official Aster docs; chain -> vault address)
EVM_VAULTS = {
    "bsc": "0x128463a60784c4d3f46c23af3f65ed859ba87974",
    "eth": "0x604dd02d620633ae427888d41bfd15e38483736e",
    "arb": "0x9e36cb86a159d479ced94fa05036f235ac40e1d5",
}
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f1"
                  "63c4a11628f55a4df523b3ef")

UA = {"User-Agent": "aster-agent-gateway/0.1",
      "Content-Type": "application/json"}

_TIMEOUT = 15.0
_RETRIES = 2  # these are secondary surfaces: fail fast, degrade honestly

_IDS = itertools.count(1)


class RpcError(Exception):
    """On-chain RPC failure with a taxonomy kind.

    kind: "rpc-limit" (local request budget / server 429), "rpc-http"
    (JSON-RPC error payload, e.g. -32005 range limit), "rpc-timeout",
    "rpc-network", "no-rpc" (env not configured - EVM only).
    """

    def __init__(self, message: str, *, kind: str,
                 status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Shared rolling-window request budgets (one per surface)

_BUDGETS: dict[str, dict] = {
    "tapi": {"limit": 30, "ts": []},
    "solana": {"limit": 10, "ts": []},
    "evm": {"limit": 20, "ts": []},
}
_BUDGET_LOCK = threading.Lock()


def requests_last_minute(surface: str) -> int:
    """Requests admitted on a surface in the rolling 60s (test hook)."""
    with _BUDGET_LOCK:
        _prune(surface, time.monotonic())
        return len(_BUDGETS[surface]["ts"])


def _prune(surface: str, now: float) -> None:
    cutoff = now - 60.0
    lst = _BUDGETS[surface]["ts"]
    while lst and lst[0] <= cutoff:
        lst.pop(0)


def _admit(surface: str) -> None:
    with _BUDGET_LOCK:
        now = time.monotonic()
        _prune(surface, now)
        b = _BUDGETS[surface]
        if len(b["ts"]) >= b["limit"]:
            raise RpcError(
                f"{surface} local request budget exhausted "
                f"({b['limit']}/min); wait for the window to roll",
                kind="rpc-limit")
        b["ts"].append(now)


# ---------------------------------------------------------------------------
# Generic JSON-RPC POST (tapi / solana / evm share the shape)

def rpc_post(url: str, method: str, params: list | dict | None = None,
             surface: str = "tapi", retries: int = _RETRIES) -> Any:
    """POST one JSON-RPC request; returns the `result` field.

    Honors JSON-RPC error payloads (code/message) and retries 429/5xx
    twice. Raises RpcError on final failure."""
    body = json.dumps({"jsonrpc": "2.0", "id": next(_IDS),
                       "method": method, "params": params or {}}).encode()
    for attempt in range(retries):
        _admit(surface)
        req = urllib.request.Request(url, data=body, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if (e.code in (429, 418) or 500 <= e.code <= 599) \
                    and attempt < retries - 1:
                ra = e.headers.get("Retry-After")
                try:
                    delay = min(30.0, float(ra)) if ra else 2.0 * (attempt + 1)
                except ValueError:
                    delay = 2.0 * (attempt + 1)
                time.sleep(delay)
                continue
            try:
                detail = e.read(300).decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise RpcError(
                f"{surface} rpc {e.code} on {method}: "
                f"{detail or e.reason}",
                kind="rpc-limit" if e.code in (429, 418) else "rpc-http",
                status=e.code) from e
        except TimeoutError as e:
            raise RpcError(f"{surface} rpc timeout on {method} after "
                           f"{_TIMEOUT}s", kind="rpc-timeout") from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if isinstance(e, urllib.error.URLError) and isinstance(
                    getattr(e, "reason", None), TimeoutError):
                raise RpcError(f"{surface} rpc timeout on {method} after "
                               f"{_TIMEOUT}s", kind="rpc-timeout") from e
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RpcError(f"{surface} rpc unreachable on {method}: {e}",
                           kind="rpc-network") from e
        except ValueError as e:
            raise RpcError(f"{surface} rpc invalid JSON on {method}: {e}",
                           kind="rpc-http") from e
        if "error" in payload:
            err = payload.get("error") or {}
            raise RpcError(
                f"{surface} rpc error on {method}: "
                f"{err.get('code')} {err.get('message')}",
                kind="rpc-http", code=err.get("code")) from None
        if "result" not in payload:
            raise RpcError(f"{surface} rpc malformed response on "
                           f"{method}: no result", kind="rpc-http")
        return payload["result"]
    raise RpcError(f"{surface} rpc failed on {method}", kind="rpc-network")


# ---------------------------------------------------------------------------
# tapi: Aster Chain account views (read-only, keyless, privacy-aware)

TAPI_METHODS = {
    "balance": "aster_getBalance",
    "openOrders": "aster_openOrders",
    "userFills": "aster_userFills",
    "spotBalance": "aster_spotGetBalance",
    "spotOpenOrders": "aster_spotOpenOrders",
    "spotUserFills": "aster_spotUserFills",
}


def tapi_call(method: str, address: str, extra: dict | None = None) -> Any:
    """One tapi read for `address`. Params use the DOCUMENTED array form
    [address, blockTag] (live-verified: the object {"userAddress": ...}
    form returns HTTP 400). extra params are ignored for now - tapi
    methods take only the two positional params. Returns the raw
    result; the SERVER layer decides privacy-empty vs data (see
    server.account_view)."""
    params: list = [(address or "").strip(), "latest"]
    return rpc_post(TAPI_URL, method, params, surface="tapi")


def tapi_balance(address: str) -> Any:
    return tapi_call("aster_getBalance", address)


def tapi_open_orders(address: str) -> Any:
    return tapi_call("aster_openOrders", address)


def tapi_user_fills(address: str) -> Any:
    return tapi_call("aster_userFills", address)


# ---------------------------------------------------------------------------
# Solana vault signatures

def solana_vault_signatures(limit: int = 20,
                            vault: str = ASTER_SOLANA_VAULT) -> list:
    """getSignaturesForAddress on the Aster Solana vault program.
    Rows: signature/slot/blockTime/err (newest first)."""
    n = max(1, min(100, int(limit)))
    return rpc_post(SOLANA_RPC, "getSignaturesForAddress",
                    [vault, {"limit": n}], surface="solana") or []


# ---------------------------------------------------------------------------
# EVM vault Transfer logs (env-gated, honest when unconfigured)

def evm_rpc_url(chain: str) -> str | None:
    """ASTER_EVM_RPC_URL_{CHAIN} wins over the generic
    ASTER_EVM_RPC_URL; None when nothing is configured."""
    chain = (chain or "").lower()
    specific = os.environ.get(f"ASTER_EVM_RPC_URL_{chain.upper()}")
    if specific:
        return specific
    return os.environ.get("ASTER_EVM_RPC_URL")


def evm_vault_transfers(chain: str, from_block: int, to_block: int,
                        limit_blocks: int = 300) -> list:
    """eth_getLogs Transfer->vault on `chain`. The window is clamped to
    `limit_blocks` (free RPCs reject wide ranges with -32005). Raises
    RpcError(kind="no-rpc") when no RPC is configured."""
    chain = (chain or "").lower()
    if chain not in EVM_VAULTS:
        raise ValueError(f"unknown chain {chain!r}: use one of "
                         f"{', '.join(sorted(EVM_VAULTS))}")
    url = evm_rpc_url(chain)
    if not url:
        raise RpcError(
            f"no RPC configured for {chain}: set ASTER_EVM_RPC_URL or "
            f"ASTER_EVM_RPC_URL_{chain.upper()} (free public RPCs reject "
            f"vault log queries with -32005, so a real endpoint is "
            f"required)", kind="no-rpc")
    vault = EVM_VAULTS[chain]
    lo = max(0, min(from_block, to_block - limit_blocks))
    hi = to_block
    logs = rpc_post(url, "eth_getLogs", [{
        "fromBlock": hex(lo), "toBlock": hex(hi),
        "address": vault,
        "topics": [TRANSFER_TOPIC, None,
                   "0x" + "0" * 24 + vault[2:]],  # Transfer(_, vault)
    }], surface="evm")
    return logs or []


def evm_latest_block(chain: str) -> int:
    """eth_blockNumber on the configured RPC (int)."""
    url = evm_rpc_url(chain)
    if not url:
        raise RpcError(f"no RPC configured for {chain}",
                       kind="no-rpc")
    res = rpc_post(url, "eth_blockNumber", [], surface="evm")
    try:
        return int(res, 16)
    except (TypeError, ValueError):
        raise RpcError(f"eth_blockNumber returned non-hex: {res!r}",
                       kind="rpc-http")


def reset_budgets() -> None:
    """Clear the per-surface request ledgers (test hook)."""
    with _BUDGET_LOCK:
        for b in _BUDGETS.values():
            b["ts"].clear()
