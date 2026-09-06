"""Unit tests for aster_mcp.chain (tapi / solana / evm) - 100% offline."""
import json

import pytest

import aster_mcp.chain as chain
from conftest import FakeResponse


def rpc_response(result):
    return {"jsonrpc": "2.0", "id": 1, "result": result}


class FakeRPC:
    """Serves scripted JSON-RPC responses; records requests."""

    def __init__(self, responses):
        # responses: list of payloads OR Exception instances
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.requests.append(body)
        if not self.responses:
            raise AssertionError("unexpected extra RPC request")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResponse(nxt)


@pytest.fixture(autouse=True)
def clean_budgets(monkeypatch):
    chain.reset_budgets()
    yield
    chain.reset_budgets()


class TestBudgets:
    def test_budget_exhausted(self, monkeypatch):
        import aster_mcp.chain as ch
        # freeze monotonic AFTER seeding: prune uses the patched clock
        # (seed > cutoff 40.0 so entries survive the prune)
        with ch._BUDGET_LOCK:
            ch._BUDGETS["tapi"]["ts"] = [41.0] * 30
        monkeypatch.setattr(ch.time, "monotonic", lambda: 100.0)
        with pytest.raises(chain.RpcError) as ei:
            ch._admit("tapi")
        assert ei.value.kind == "rpc-limit"

    def test_budget_counts_requests(self):
        chain.reset_budgets()
        import aster_mcp.chain as ch
        with ch._BUDGET_LOCK:
            ch._BUDGETS["solana"]["ts"] = [1.0, 2.0]
        assert ch.requests_last_minute("solana") in (0, 2)
        chain.reset_budgets()


class TestTapi:
    def test_balance_call_shape(self, monkeypatch):
        fake = FakeRPC([rpc_response({"balances": []})])
        monkeypatch.setattr("urllib.request.urlopen", fake)
        chain.tapi_balance("0x" + "ab" * 20)
        body = fake.requests[0]
        assert body["method"] == "aster_getBalance"
        assert body["params"]["userAddress"] == "0x" + "ab" * 20
        assert body["jsonrpc"] == "2.0"

    def test_jsonrpc_error_raises(self, monkeypatch):
        err = {"jsonrpc": "2.0", "id": 1,
               "error": {"code": -32601, "message": "method not found"}}
        fake = FakeRPC([err])
        monkeypatch.setattr("urllib.request.urlopen", fake)
        with pytest.raises(chain.RpcError) as ei:
            chain.tapi_balance("0x" + "ab" * 20)
        assert ei.value.kind == "rpc-http"
        assert ei.value.code == -32601

    def test_method_map(self):
        assert chain.TAPI_METHODS["balance"] == "aster_getBalance"
        assert chain.TAPI_METHODS["openOrders"] == "aster_openOrders"
        assert chain.TAPI_METHODS["userFills"] == "aster_userFills"
        assert chain.TAPI_METHODS["spotBalance"] == "aster_spotGetBalance"


class TestSolana:
    def test_signatures_shape(self, monkeypatch):
        sigs = [{"signature": "x" * 64, "slot": 1, "blockTime": 2,
                 "err": None}]
        fake = FakeRPC([rpc_response(sigs)])
        monkeypatch.setattr("urllib.request.urlopen", fake)
        out = chain.solana_vault_signatures(20)
        assert out == sigs
        method = fake.requests[0]["method"]
        assert method == "getSignaturesForAddress"
        vault = fake.requests[0]["params"][0]
        assert vault == chain.ASTER_SOLANA_VAULT

    def test_limit_clamped(self, monkeypatch):
        fake = FakeRPC([rpc_response([])])
        monkeypatch.setattr("urllib.request.urlopen", fake)
        chain.solana_vault_signatures(5000)
        assert fake.requests[0]["params"][1]["limit"] == 100


class TestEVM:
    def test_no_rpc_configured_honest(self, monkeypatch):
        monkeypatch.delenv("ASTER_EVM_RPC_URL", raising=False)
        monkeypatch.delenv("ASTER_EVM_RPC_URL_BSC", raising=False)
        with pytest.raises(chain.RpcError) as ei:
            chain.evm_vault_transfers("bsc", 1000, 1300)
        assert ei.value.kind == "no-rpc"
        assert "no RPC configured" in str(ei.value)

    def test_per_chain_env_override(self, monkeypatch):
        monkeypatch.setenv("ASTER_EVM_RPC_URL_BSC", "https://bsc.example")
        monkeypatch.delenv("ASTER_EVM_RPC_URL", raising=False)
        assert chain.evm_rpc_url("bsc") == "https://bsc.example"
        assert chain.evm_rpc_url("eth") is None

    def test_generic_env_fallback(self, monkeypatch):
        monkeypatch.setenv("ASTER_EVM_RPC_URL", "https://gen.example")
        monkeypatch.delenv("ASTER_EVM_RPC_URL_ARB", raising=False)
        assert chain.evm_rpc_url("arb") == "https://gen.example"

    def test_getlogs_request_shape(self, monkeypatch):
        logs = [{"transactionHash": "0x1", "blockNumber": "0x10",
                 "topics": [chain.TRANSFER_TOPIC, None, "0x" + "0"*24],
                 "address": chain.EVM_VAULTS["bsc"]}]
        fake = FakeRPC([rpc_response(logs)])
        monkeypatch.setattr("urllib.request.urlopen", fake)
        monkeypatch.setenv("ASTER_EVM_RPC_URL_BSC", "https://bsc.example")
        out = chain.evm_vault_transfers("bsc", 1000, 1300)
        assert out == logs
        p = fake.requests[0]["params"][0]
        assert p["address"] == chain.EVM_VAULTS["bsc"]
        assert p["topics"][0] == chain.TRANSFER_TOPIC
        # to-topic is the vault address right-padded
        assert p["topics"][2].endswith(chain.EVM_VAULTS["bsc"][2:])

    def test_32005_range_error_taxonomy(self, monkeypatch):
        err = {"jsonrpc": "2.0", "id": 1,
               "error": {"code": -32005, "message": "limit exceeded"}}
        fake = FakeRPC([err, err])  # 2 attempts (retries=2)
        monkeypatch.setattr("urllib.request.urlopen", fake)
        monkeypatch.setenv("ASTER_EVM_RPC_URL", "https://gen.example")
        import aster_mcp.chain as ch
        monkeypatch.setattr(ch.time, "sleep", lambda s: None)
        with pytest.raises(chain.RpcError) as ei:
            chain.evm_vault_transfers("eth", 1000, 1300)
        assert ei.value.kind == "rpc-http"
        assert ei.value.code == -32005

    def test_unknown_chain_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown chain"):
            chain.evm_vault_transfers("matic", 1, 2)

    def test_vault_addresses_pinned(self):
        assert chain.EVM_VAULTS["bsc"].startswith("0x128463a6")
        assert chain.EVM_VAULTS["eth"].startswith("0x604dd02d")
        assert chain.EVM_VAULTS["arb"].startswith("0x9e36cb86")

    def test_latest_block_hex_parse(self, monkeypatch):
        fake = FakeRPC([rpc_response("0x1d4c00")])
        monkeypatch.setattr("urllib.request.urlopen", fake)
        monkeypatch.setenv("ASTER_EVM_RPC_URL", "https://gen.example")
        assert chain.evm_latest_block("bsc") == int("1d4c00", 16)
