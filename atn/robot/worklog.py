"""Worklog attestation (docs/robots.md, decision 6).

A challenger sends a random challenge. The robot answers with a statement
signed by its agent key over the challenge, the time it answered, the active
pack digests and the sha256 of every closed MCAP worklog chunk inside the
window. The challenger judges timeliness by its OWN clock (when the answer
arrived), never by the robot's ``responded_at``.

The chunk currently being written is left out: its bytes are still changing,
so its hash could never be checked later.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

STATEMENT_TYPE = "autonet.robot.attestation/1"
CHUNK_SUFFIX = ".mcap"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def closed_chunks(bag_dir: str | Path, window_s: float,
                  now: float | None = None) -> list[Path]:
    """Closed MCAP chunks modified within ``window_s``, oldest first. The
    newest chunk overall is the one being written and is excluded."""
    now = time.time() if now is None else now
    files = sorted(Path(bag_dir).rglob(f"*{CHUNK_SUFFIX}"),
                   key=lambda p: p.stat().st_mtime)
    closed = files[:-1]
    return [p for p in closed if now - p.stat().st_mtime <= window_s]


def canonical_statement_bytes(statement: dict[str, Any]) -> bytes:
    body = {k: v for k, v in statement.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_statement(*, robot: str, challenge: str, packs: list[str],
                    bag_dir: str | Path, window_s: float,
                    now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    chunks = [{"name": p.name, "sha256": _sha256_file(p),
               "bytes": p.stat().st_size}
              for p in closed_chunks(bag_dir, window_s, now)]
    return {"type": STATEMENT_TYPE, "robot": robot, "challenge": challenge,
            "responded_at": now, "window_s": window_s,
            "packs": list(packs), "chunks": chunks}


def sign_statement(statement: dict[str, Any], private_key: str) -> dict[str, Any]:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    signed = Account.sign_message(
        encode_defunct(canonical_statement_bytes(statement)),
        private_key=private_key)
    return {**statement, "sig": signed.signature.hex()}


def recover_signer(statement: dict[str, Any]) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    sig = statement.get("sig") or ""
    return Account.recover_message(
        encode_defunct(canonical_statement_bytes(statement)),
        signature=bytes.fromhex(sig.removeprefix("0x")))


def verify_statement(statement: dict[str, Any], *, robot: str, challenge: str,
                     sent_at: float, received_at: float, deadline_s: float,
                     required_packs: list[str] | None = None,
                     min_chunks: int = 1,
                     chunk_files: dict[str, bytes] | None = None,
                     ) -> list[str]:
    """Why the statement fails (empty list = it passes).

    ``chunk_files`` (name -> bytes), when the challenger fetched the chunks,
    are checked against the signed hashes. Witness cross-checks over the
    chunk contents are outside this function."""
    errors = []
    if statement.get("type") != STATEMENT_TYPE:
        errors.append("wrong statement type")
    if statement.get("challenge") != challenge:
        errors.append("challenge mismatch")
    if received_at - sent_at > deadline_s:
        errors.append(f"late: {received_at - sent_at:.2f}s > {deadline_s:.2f}s")
    try:
        signer = recover_signer(statement)
        if signer.lower() != robot.lower():
            errors.append("signed by a different key")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bad signature: {exc}")
    if required_packs is not None and sorted(statement.get("packs") or []) != sorted(required_packs):
        errors.append("active packs differ from the required set")
    chunks = statement.get("chunks") or []
    if len(chunks) < min_chunks:
        errors.append(f"{len(chunks)} worklog chunks in window, need {min_chunks}")
    for c in chunks:
        if chunk_files is None or c.get("name") not in chunk_files:
            continue
        if hashlib.sha256(chunk_files[c["name"]]).hexdigest() != c.get("sha256"):
            errors.append(f"chunk {c.get('name')} does not match its hash")
    return errors
