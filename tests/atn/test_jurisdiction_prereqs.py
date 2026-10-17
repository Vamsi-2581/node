"""One jurisdiction at a time, resolved from one Registry (docs/
multi_jurisdiction.md decisions 2-5): the seed parser, the refuse-to-start
prerequisites, the epoch rules version, the jurisdiction key, and the
charter check."""
from __future__ import annotations

import hashlib
import json

from atn.autonet_service import AutonetBridge
from atn.config import (
    RPBConfig, _parse_registry_data, epoch_rules_error, jurisdiction_key,
    missing_prerequisites,
)
from nodes.common.world_model_substrate.adapter import CHARTER

V1 = json.dumps({"axes": [{"id": c["id"], "thesis": c["thesis"]} for c in CHARTER]},
                separators=(",", ":"))

FULL = {
    "dao": "0xDA0", "timelock": "0x71", "rep_token": "0x2E9", "tool_bond": "0xB0",
    "substrate": "0x5B", "registry": "0x2E6", "epoch_rules": "1", "charter": V1,
}


def _registry(contracts, chain_id=31337):
    return {"jurisdictions": {"autonet": {
        "network": {"rpc_url": "http://x", "chain_id": chain_id},
        "contracts": contracts}}}


def _config(contracts, joined=True, chain_id=31337):
    seed = _parse_registry_data(_registry(contracts, chain_id), "autonet")
    an = RPBConfig()
    for k, v in seed.items():
        if hasattr(an, k):
            setattr(an, k, v)
    an.network_joined = joined
    return an


def _service(an):
    return AutonetBridge(an)


def test_seed_keeps_timelock_registry_rules_and_charter_text():
    seed = _parse_registry_data(_registry(FULL), "autonet")
    assert seed["timelock_address"] == "0x71"
    assert seed["dao_registry_address"] == "0x2E6"
    assert seed["epoch_rules"] == "1"
    assert seed["charter_text"] == V1
    # The exact registry bytes are hashed, never a re-serialization.
    assert seed["charter_hash"] == "0x" + hashlib.sha256(V1.encode()).hexdigest()


def test_registry_json_name_for_the_dao_registry():
    seed = _parse_registry_data(_registry({**FULL, "registry": "", "dao_registry": "0xD2"}),
                                "autonet")
    assert seed["dao_registry_address"] == "0xD2"


def test_missing_prerequisites_named():
    an = _config({k: v for k, v in FULL.items() if k not in ("rep_token", "timelock")})
    assert missing_prerequisites(an) == ["rep_token", "timelock"]
    assert missing_prerequisites(_config(FULL)) == []


def test_epoch_rules():
    assert epoch_rules_error(_config(FULL)) == ""
    assert epoch_rules_error(_config({**FULL, "epoch_rules": ""})) == ""   # "1"
    assert "version 2" in epoch_rules_error(_config({**FULL, "epoch_rules": "2"}))


def test_jurisdiction_key_is_chain_and_dao():
    assert jurisdiction_key(_config(FULL, chain_id=127823)) == "127823:0xda0"
    unset = RPBConfig()
    unset.dao_address, unset.chain_id = "", 0
    assert jurisdiction_key(unset) == ""


def test_service_refuses_without_prerequisites():
    svc = _service(_config({k: v for k, v in FULL.items() if k != "tool_bond"}))
    svc._mirror_jurisdiction()
    assert svc.state.jurisdiction_errors == [
        "missing jurisdiction addresses: tool_bond"]
    assert svc.state.to_dict()["jurisdiction_key"] == "31337:0xda0"


def test_service_ok_with_full_jurisdiction():
    svc = _service(_config(FULL))
    svc._mirror_jurisdiction()
    assert svc.state.jurisdiction_errors == []
    assert svc.state.charter_match is True


def test_no_errors_before_joining():
    svc = _service(_config({}, joined=False))
    svc._mirror_jurisdiction()
    assert svc.state.jurisdiction_errors == []


def test_charter_without_mandatory_axes_refused():
    bad = json.dumps({"axes": [{"id": "life_precious", "thesis": "x"}]})
    svc = _service(_config({**FULL, "charter": bad}))
    svc._mirror_jurisdiction()
    assert any("mandatory axes" in e for e in svc.state.jurisdiction_errors)


def test_custom_charter_refused_until_supported():
    custom = json.dumps({"axes": [{"id": "correctness", "thesis": "a"},
                                  {"id": "simplicity", "thesis": "b"},
                                  {"id": "kindness", "thesis": "c"}]})
    svc = _service(_config({**FULL, "charter": custom}))
    svc._mirror_jurisdiction()
    assert svc.state.charter_match is False
    assert any("differs" in e for e in svc.state.jurisdiction_errors)
