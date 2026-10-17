"""Staked review batches (docs/multi_jurisdiction.md decision 10): score and
harm encoding, the commitment ToolBond checks, and the per-epoch batch
store."""
from __future__ import annotations

import pytest

from atn.runtime.review_batch import (
    NO_HARM, NO_SCORE, EpochBatch, ReviewBatchStore, ReviewEntry,
    commitment_of, encode_harm, encode_scores,
)

AXES = ["life_precious", "self_preservation", "promotion_of_intelligence",
        "evolution", "correctness", "simplicity"]
D1 = "aa" * 32
D2 = "bb" * 32
AGENT = "0x" + "12" * 20


def test_scores_one_int8_per_axis_in_charter_order():
    b = encode_scores({"correctness": 0.5, "simplicity": -1.0}, AXES)
    assert len(b) == 6
    vals = [x - 256 if x > 127 else x for x in b]
    assert vals == [NO_SCORE, NO_SCORE, NO_SCORE, NO_SCORE, 50, -100]


def test_scores_clamped_and_empty_when_nothing_scored():
    vals = [x - 256 if x > 127 else x for x in encode_scores({"correctness": 3.0}, AXES)]
    assert vals[4] == 100
    assert encode_scores({}, AXES) == b""
    assert encode_scores({"unknown_axis": 1.0}, AXES) == b""


def test_harm_encoding():
    assert encode_harm(None) == NO_HARM
    assert encode_harm(0.0) == 0
    assert encode_harm(0.804) == 80
    assert encode_harm(2) == 100


def test_too_many_axes_rejected():
    with pytest.raises(ValueError):
        encode_scores({"a": 1.0}, [f"a{i}" for i in range(17)])


def test_commitment_matches_abi_encoding():
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address
    entry = ReviewEntry(digest=D1, axes={"correctness": 1.0}, harm=0.1, stake=5)
    inputs = [entry.to_input(AXES)]
    salt = b"\x07" * 32
    want = keccak(encode(["uint256", "address", "(bytes32,bytes,uint8,uint256,bytes32)[]", "bytes32"],
                         [3, to_checksum_address(AGENT), inputs, salt]))
    assert commitment_of(3, AGENT, inputs, salt) == want
    # Any change to the hidden content changes the commitment.
    other = [ReviewEntry(digest=D1, axes={"correctness": 0.9}, harm=0.1, stake=5).to_input(AXES)]
    assert commitment_of(3, AGENT, other, salt) != want


def test_store_queues_seals_and_rolls_to_next_epoch(tmp_path):
    store = ReviewBatchStore(tmp_path, "31337:0xdao")
    store.add(AGENT, 4, AXES, ReviewEntry(digest=D1, axes={"correctness": 1.0}, stake=2))
    store.add(AGENT, 4, AXES, ReviewEntry(digest=D2, harm=0.9, stake=3))
    # A second review of the same tool replaces the first.
    store.add(AGENT, 4, AXES, ReviewEntry(digest=D1, axes={"correctness": 0.2}, stake=1))
    b = store.load(AGENT, 4, AXES)
    assert [r.digest for r in b.reviews] == [D2, D1]
    assert b.total_stake == 4

    # Once committed, the batch is sealed: new reviews go to the next epoch.
    b.salt, b.commitment = "ab" * 32, "cd" * 32
    store.save(b)
    nxt = store.add(AGENT, 4, AXES, ReviewEntry(digest=D1, stake=1))
    assert nxt.epoch == 5
    assert [x.epoch for x in store.unrevealed()] == [4]

    sealed = store.load(AGENT, 4, AXES).sealed_reveal()
    assert sealed["salt"] == "ab" * 32 and len(sealed["reviews"]) == 2


def test_batches_are_kept_per_jurisdiction(tmp_path):
    a = ReviewBatchStore(tmp_path, "31337:0xa")
    b = ReviewBatchStore(tmp_path, "31337:0xb")
    a.add(AGENT, 1, AXES, ReviewEntry(digest=D1, stake=1))
    assert b.load(AGENT, 1, AXES).reviews == []
    assert isinstance(a.load(AGENT, 1, AXES), EpochBatch)
