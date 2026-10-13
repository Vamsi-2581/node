"""A launched jurisdiction's contracts can come from its DAO Registry on chain
(ATN_JURISDICTION_REGISTRY), so joining it needs no registry.json release."""
import atn.config as cfg


def test_onchain_registry_used_when_env_set(monkeypatch):
    monkeypatch.delenv("ATN_REGISTRY_URL", raising=False)
    monkeypatch.setenv("ATN_JURISDICTION_REGISTRY", "0x" + "11" * 20)
    seen = {}

    def fake(addr, rpc, jid):
        seen.update(addr=addr, rpc=rpc, jid=jid)
        return {"jurisdictions": {jid: {
            "network": {"rpc_url": rpc, "chain_id": 7},
            "contracts": {"substrate": "0x" + "22" * 20, "tool_bond": "0x" + "33" * 20,
                          "dao_registry": addr}}}}

    monkeypatch.setattr(cfg, "_fetch_onchain_registry", fake)
    seed = cfg._load_registry_seed("launched")
    assert seen["jid"] == "launched"
    assert seed["substrate_address"] == "0x" + "22" * 20
    assert seed["tool_bond_address"] == "0x" + "33" * 20
    assert seed["chain_id"] == 7


def test_unreadable_onchain_registry_falls_back_to_packaged(monkeypatch):
    monkeypatch.delenv("ATN_REGISTRY_URL", raising=False)
    monkeypatch.setenv("ATN_JURISDICTION_REGISTRY", "0x" + "11" * 20)
    monkeypatch.setattr(cfg, "_fetch_onchain_registry", lambda *a: None)
    monkeypatch.delenv("ATN_JURISDICTION_RPC", raising=False)
    seed = cfg._load_registry_seed("autonet")
    assert seed.get("substrate_address")


def test_no_env_means_packaged(monkeypatch):
    monkeypatch.delenv("ATN_REGISTRY_URL", raising=False)
    monkeypatch.delenv("ATN_JURISDICTION_REGISTRY", raising=False)

    def boom(*a):
        raise AssertionError("must not read the chain")

    monkeypatch.setattr(cfg, "_fetch_onchain_registry", boom)
    assert cfg._load_registry_seed("autonet").get("substrate_address")
