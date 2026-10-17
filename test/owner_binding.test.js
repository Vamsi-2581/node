const { expect } = require("chai");
const { ethers } = require("hardhat");

const digest = (x) => ethers.keccak256(ethers.toUtf8Bytes(x));
const { registerAgent, signBinding, signRevoke } = require("./helpers");

describe("Substrate owner-rooted registration (OwnerBound)", function () {
  let substrate;
  let deployer, ownerW, otherOwnerW, agentA, agentB, agentC, stranger;
  const ZERO = ethers.ZeroAddress;

  beforeEach(async function () {
    [deployer, ownerW, otherOwnerW, agentA, agentB, agentC, stranger] =
      await ethers.getSigners();
    substrate = await require("./helpers").deploySubstrate();
    await substrate.waitForDeployment();
  });

  // ---- happy path: fresh bound registration ------------------------------

  it("fresh top-level bound registration binds owner, emits both events", async function () {
    const sig = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await expect(
      substrate
        .connect(agentA)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          ZERO,
          sig
        )
    )
      .to.emit(substrate, "AgentRegistered")
      .and.to.emit(substrate, "OwnerBound")
      .withArgs(agentA.address, ownerW.address, ZERO, ZERO, 0);

    expect(await substrate.isRegistered(agentA.address)).to.equal(true);
    expect(await substrate.getAgentOwner(agentA.address)).to.equal(ownerW.address);
    expect(await substrate.getAgentParent(agentA.address)).to.equal(ZERO);
    // nonce advanced past the consumed 0.
    expect(await substrate.bindingNonce(agentA.address)).to.equal(1);
  });

  it("fresh bound registration with a parent under the same owner", async function () {
    // Parent A first (top-level).
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );

    // Child B under A, same owner.
    const sigB = await signBinding(
      substrate,
      ownerW,
      agentB.address,
      agentA.address
    );
    await expect(
      substrate
        .connect(agentB)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          agentA.address,
          sigB
        )
    )
      .to.emit(substrate, "OwnerBound")
      .withArgs(agentB.address, ownerW.address, agentA.address, ZERO, 0);

    expect(await substrate.getAgentParent(agentB.address)).to.equal(agentA.address);
    expect(await substrate.getAgentOwner(agentB.address)).to.equal(ownerW.address);
  });

  // ---- bad signature paths -----------------------------------------------

  it("wrong signer (not the claimed owner) reverts", async function () {
    // otherOwnerW signs, but we claim ownerW as owner.
    const sig = await signBinding(substrate, otherOwnerW, agentA.address, ZERO);
    await expect(
      substrate
        .connect(agentA)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          ZERO,
          sig
        )
    ).to.be.revertedWithCustomError(substrate, "BadOwnerBindingSignature");
  });

  it("tampered agent address in sig (signed for a different agent) reverts", async function () {
    // owner signed for agentB, but agentA submits.
    const sig = await signBinding(substrate, ownerW, agentB.address, ZERO);
    await expect(
      substrate
        .connect(agentA)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          ZERO,
          sig
        )
    ).to.be.revertedWithCustomError(substrate, "BadOwnerBindingSignature");
  });

  it("wrong parent in sig vs arg reverts", async function () {
    // Register A top-level so it's a valid same-owner parent candidate.
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );
    // owner signs B with parent = ZERO, but B submits parent = agentA.
    const sig = await signBinding(substrate, ownerW, agentB.address, ZERO);
    await expect(
      substrate
        .connect(agentB)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          agentA.address,
          sig
        )
    ).to.be.revertedWithCustomError(substrate, "BadOwnerBindingSignature");
  });

  it("wrong nonce in sig reverts (stale/future nonce)", async function () {
    // owner signs at nonce 1, but the agent's current nonce is 0.
    const sig = await signBinding(substrate, ownerW, agentA.address, ZERO, 1);
    await expect(
      substrate
        .connect(agentA)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          ZERO,
          sig
        )
    ).to.be.revertedWithCustomError(substrate, "BadOwnerBindingSignature");
  });

  it("owner = address(0) reverts (OwnerRequired)", async function () {
    const sig = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await expect(
      substrate
        .connect(agentA)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ZERO,
          ZERO,
          sig
        )
    ).to.be.revertedWithCustomError(substrate, "OwnerRequired");
  });

  // ---- parent constraint paths -------------------------------------------

  it("parent not registered reverts", async function () {
    // agentC is never registered; B claims it as parent.
    const sig = await signBinding(
      substrate,
      ownerW,
      agentB.address,
      agentC.address
    );
    await expect(
      substrate
        .connect(agentB)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          agentC.address,
          sig
        )
    ).to.be.revertedWithCustomError(substrate, "ParentNotRegistered");
  });

  it("parent with a DIFFERENT owner reverts", async function () {
    // A belongs to ownerW.
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );
    // B claims otherOwnerW as owner but parent A (owned by ownerW).
    const sigB = await signBinding(
      substrate,
      otherOwnerW,
      agentB.address,
      agentA.address
    );
    await expect(
      substrate
        .connect(agentB)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          otherOwnerW.address,
          agentA.address,
          sigB
        )
    ).to.be.revertedWithCustomError(substrate, "ParentOwnerMismatch");
  });

  // ---- owner rotation (key-loss recovery) ---------------------------------

  it("rotateOwner: leaked-owner recovery — rotate A→ownerB WITHOUT ownerA participating", async function () {
    // A is bound to ownerW (the wallet whose key later leaks).
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );
    expect(await substrate.getAgentOwner(agentA.address)).to.equal(ownerW.address);
    expect(await substrate.bindingNonce(agentA.address)).to.equal(1);

    // Recovery: the daemon host (holds agent A's key) rotates to a fresh
    // wallet otherOwnerW. ONLY the NEW owner signs; ownerW never participates.
    const sigRot = await signBinding(substrate, otherOwnerW, agentA.address, ZERO);
    await expect(
      substrate.connect(agentA).rotateOwner(otherOwnerW.address, ZERO, sigRot)
    )
      .to.emit(substrate, "OwnerBound")
      // prevOwner is the leaked ownerW; nonce consumed is 1.
      .withArgs(agentA.address, otherOwnerW.address, ZERO, ownerW.address, 1);

    expect(await substrate.getAgentOwner(agentA.address)).to.equal(
      otherOwnerW.address
    );
    expect(await substrate.bindingNonce(agentA.address)).to.equal(2);
  });

  it("CRITICAL replay: a captured OLD binding sig cannot rotate back after rotation", async function () {
    // Bind A → ownerW. Capture ownerW's signature (the "old" leaked-owner sig).
    const oldSig = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        oldSig
      );

    // Recover away from the leaked ownerW to otherOwnerW.
    const sigRot = await signBinding(substrate, otherOwnerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .rotateOwner(otherOwnerW.address, ZERO, sigRot);
    expect(await substrate.getAgentOwner(agentA.address)).to.equal(
      otherOwnerW.address
    );

    // The thief replays the captured oldSig (signed at nonce 0) to rotate the
    // fleet BACK to the leaked ownerW. Nonce is now 2, so it recovers to a
    // different (non-owner) signer and is rejected.
    await expect(
      substrate.connect(agentA).rotateOwner(ownerW.address, ZERO, oldSig)
    ).to.be.revertedWithCustomError(substrate, "BadOwnerBindingSignature");

    // Ownership unchanged: recovery holds.
    expect(await substrate.getAgentOwner(agentA.address)).to.equal(
      otherOwnerW.address
    );
  });

  it("rotateOwner on an unregistered agent reverts (AgentNotActive)", async function () {
    const sig = await signBinding(substrate, ownerW, stranger.address, ZERO);
    await expect(
      substrate.connect(stranger).rotateOwner(ownerW.address, ZERO, sig)
    ).to.be.revertedWithCustomError(substrate, "AgentNotActive");
  });

  it("rotation parent rules: new parent must be owned by the NEW owner", async function () {
    // A bound to ownerW top-level; B bound to ownerW under A.
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );
    const sigB = await signBinding(substrate, ownerW, agentB.address, agentA.address);
    await substrate
      .connect(agentB)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        agentA.address,
        sigB
      );

    // Migrate the fleet to otherOwnerW. Rotate the PARENT first.
    const sigRotA = await signBinding(substrate, otherOwnerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .rotateOwner(otherOwnerW.address, ZERO, sigRotA);

    // Attempt to rotate B to otherOwnerW while keeping parent A BEFORE A is
    // under the new owner would fail; but A is now under otherOwnerW, so
    // rotating B under A succeeds. First prove the failure mode: rotating B
    // under a parent still owned by the OLD owner.
    // (Here A is already migrated, so use a fresh mismatch: parent = agentA is
    // fine now. Demonstrate mismatch via a parent owned by nobody-new.)
    // Rotate B under the now-migrated A — happy path.
    const sigRotB = await signBinding(
      substrate,
      otherOwnerW,
      agentB.address,
      agentA.address
    );
    await substrate
      .connect(agentB)
      .rotateOwner(otherOwnerW.address, agentA.address, sigRotB);
    expect(await substrate.getAgentOwner(agentB.address)).to.equal(
      otherOwnerW.address
    );
    expect(await substrate.getAgentParent(agentB.address)).to.equal(agentA.address);
  });

  it("rotation parent rules: parent owned by a different owner reverts", async function () {
    // A bound to ownerW; C bound to otherOwnerW. Try to rotate C's child under
    // A (owned by ownerW) while claiming otherOwnerW as owner → mismatch.
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );
    // B is a fresh top-level agent under otherOwnerW; rotating it under parent A
    // (owned by ownerW) while keeping otherOwnerW as owner -> ParentOwnerMismatch.
    await registerAgent(substrate, agentB, "lineB", otherOwnerW);
    const sigB = await signBinding(
      substrate,
      otherOwnerW,
      agentB.address,
      agentA.address
    );
    await expect(
      substrate.connect(agentB).rotateOwner(otherOwnerW.address, agentA.address, sigB)
    ).to.be.revertedWithCustomError(substrate, "ParentOwnerMismatch");
  });

  it("children keep their own bindings when a parent rotates (per-agent rotation)", async function () {
    // A (parent) and B (child under A), both under ownerW.
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );
    const sigB = await signBinding(substrate, ownerW, agentB.address, agentA.address);
    await substrate
      .connect(agentB)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        agentA.address,
        sigB
      );

    // Rotate only A to otherOwnerW. B is untouched: its owner/parent stand.
    const sigRotA = await signBinding(substrate, otherOwnerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .rotateOwner(otherOwnerW.address, ZERO, sigRotA);

    expect(await substrate.getAgentOwner(agentA.address)).to.equal(
      otherOwnerW.address
    );
    // B still points at ownerW and parent A — a fleet migrates agent by agent.
    expect(await substrate.getAgentOwner(agentB.address)).to.equal(ownerW.address);
    expect(await substrate.getAgentParent(agentB.address)).to.equal(agentA.address);
  });

  // ---- nonce view ---------------------------------------------------------

  it("bindingNonce view starts at 0 and increments on each bind/rotate", async function () {
    expect(await substrate.bindingNonce(agentA.address)).to.equal(0);
    const sig0 = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sig0
      );
    expect(await substrate.bindingNonce(agentA.address)).to.equal(1);

    const sig1 = await signBinding(substrate, otherOwnerW, agentA.address, ZERO);
    await substrate.connect(agentA).rotateOwner(otherOwnerW.address, ZERO, sig1);
    expect(await substrate.bindingNonce(agentA.address)).to.equal(2);

    // sameOwner reflects the rotation immediately.
    // Bind B to otherOwnerW; A (rotated) and B now share an owner.
    const sigB = await signBinding(substrate, otherOwnerW, agentB.address, ZERO);
    await substrate
      .connect(agentB)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        otherOwnerW.address,
        ZERO,
        sigB
      );
    expect(await substrate.sameOwner(agentA.address, agentB.address)).to.equal(true);
  });

  it("bound registration cannot re-register an already-registered agent", async function () {
    await registerAgent(substrate, agentA, "lineA", ownerW);
    const sig = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await expect(
      substrate
        .connect(agentA)
        .registerAgentBound(
          ethers.toUtf8Bytes("peer-x"),
          ownerW.address,
          ZERO,
          sig
        )
    ).to.be.revertedWithCustomError(substrate, "AlreadyRegistered");
  });

  // ---- sameOwner truth table ---------------------------------------------

  it("sameOwner truth table (unregistered addresses have no owner)", async function () {
    // A and B under ownerW; C under otherOwnerW; stranger unset.
    const sigA = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigA
      );
    const sigB = await signBinding(substrate, ownerW, agentB.address, ZERO);
    await substrate
      .connect(agentB)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sigB
      );
    const sigC = await signBinding(substrate, otherOwnerW, agentC.address, ZERO);
    await substrate
      .connect(agentC)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        otherOwnerW.address,
        ZERO,
        sigC
      );

    // same owner → true
    expect(await substrate.sameOwner(agentA.address, agentB.address)).to.equal(true);
    expect(await substrate.sameOwner(agentB.address, agentA.address)).to.equal(true);
    // self is same owner
    expect(await substrate.sameOwner(agentA.address, agentA.address)).to.equal(true);
    // different owners → false
    expect(await substrate.sameOwner(agentA.address, agentC.address)).to.equal(false);
    // one side unset → false (both directions)
    expect(await substrate.sameOwner(agentA.address, stranger.address)).to.equal(false);
    expect(await substrate.sameOwner(stranger.address, agentA.address)).to.equal(false);
    // both unset → false
    expect(await substrate.sameOwner(stranger.address, deployer.address)).to.equal(false);
  });

  it("registerTool unaffected: bound agent can register a tool", async function () {
    const sig = await signBinding(substrate, ownerW, agentA.address, ZERO);
    await substrate
      .connect(agentA)
      .registerAgentBound(
        ethers.toUtf8Bytes("peer-x"),
        ownerW.address,
        ZERO,
        sig
      );
    const d = digest("manifest-1");
    await expect(substrate.connect(agentA).registerTool(d)).to.emit(
      substrate,
      "ToolRegistered"
    );
    expect(await substrate.toolAuthor(d)).to.equal(agentA.address);
  });

  // ---- parent-signed registration ----------------------------------------

  async function regTop(agent, owner = ownerW) {
    await registerAgent(substrate, agent, "top", owner);
  }

  it("parent-signed child registers into the parent's household at depth + 1", async function () {
    await regTop(agentA);
    expect(await substrate.agentDepth(agentA.address)).to.equal(0);
    await expect(
      registerAgent(substrate, agentB, "kid", ownerW, agentA.address, agentA)
    )
      .to.emit(substrate, "OwnerBound")
      .withArgs(agentB.address, ownerW.address, agentA.address, ZERO, 0);
    expect(await substrate.getAgentOwner(agentB.address)).to.equal(ownerW.address);
    expect(await substrate.agentDepth(agentB.address)).to.equal(1);
    expect(await substrate.sameOwner(agentA.address, agentB.address)).to.equal(true);
    // grandchild
    await registerAgent(substrate, agentC, "grand", ownerW, agentB.address, agentB);
    expect(await substrate.agentDepth(agentC.address)).to.equal(2);
  });

  it("a parent-signed signature cannot rotateOwner", async function () {
    await regTop(agentA);
    await registerAgent(substrate, agentB, "kid", ownerW, agentA.address, agentA);
    // parent A signs B's rotation (parent 0): rejected, only the new owner may sign.
    const sig = await signBinding(substrate, agentA, agentB.address, ZERO);
    await expect(
      substrate.connect(agentB).rotateOwner(ownerW.address, ZERO, sig)
    ).to.be.revertedWithCustomError(substrate, "BadOwnerBindingSignature");
  });

  it("a stranger's signature with parent set reverts", async function () {
    await regTop(agentA);
    await expect(
      registerAgent(substrate, agentB, "kid", ownerW, agentA.address, stranger)
    ).to.be.revertedWithCustomError(substrate, "BadOwnerBindingSignature");
  });

  it("child of a revoked parent cannot register (AgentNotActive)", async function () {
    await regTop(agentA);
    await substrate.connect(ownerW).revokeAgent(agentA.address, "0x");
    await expect(
      registerAgent(substrate, agentB, "kid", ownerW, agentA.address, agentA)
    ).to.be.revertedWithCustomError(substrate, "AgentNotActive");
  });

  // ---- revocation --------------------------------------------------------

  it("owner revokes directly with an empty signature", async function () {
    await regTop(agentA);
    expect(await substrate.isLive(agentA.address)).to.equal(true);
    await expect(substrate.connect(ownerW).revokeAgent(agentA.address, "0x"))
      .to.emit(substrate, "AgentRevoked")
      .withArgs(agentA.address, ownerW.address);
    expect(await substrate.isLive(agentA.address)).to.equal(false);
  });

  it("anyone can relay the owner's revoke signature", async function () {
    await regTop(agentA);
    const sig = await signRevoke(substrate, ownerW, agentA.address);
    await expect(substrate.connect(stranger).revokeAgent(agentA.address, sig))
      .to.emit(substrate, "AgentRevoked")
      .withArgs(agentA.address, ownerW.address);
    expect(await substrate.isLive(agentA.address)).to.equal(false);
  });

  it("revoke with a bad signature reverts (BadRevokeSignature)", async function () {
    await regTop(agentA);
    const sig = await signRevoke(substrate, otherOwnerW, agentA.address);
    await expect(
      substrate.connect(stranger).revokeAgent(agentA.address, sig)
    ).to.be.revertedWithCustomError(substrate, "BadRevokeSignature");
    // owner's signature, but for a different agent
    const sigB = await signRevoke(substrate, ownerW, agentB.address);
    await expect(
      substrate.connect(stranger).revokeAgent(agentA.address, sigB)
    ).to.be.revertedWithCustomError(substrate, "BadRevokeSignature");
  });

  it("non-owner sender with an empty signature reverts", async function () {
    await regTop(agentA);
    await expect(
      substrate.connect(stranger).revokeAgent(agentA.address, "0x")
    ).to.be.reverted;
    expect(await substrate.isLive(agentA.address)).to.equal(true);
  });

  it("double revoke reverts (AgentNotActive); unregistered agent too", async function () {
    await regTop(agentA);
    await substrate.connect(ownerW).revokeAgent(agentA.address, "0x");
    await expect(
      substrate.connect(ownerW).revokeAgent(agentA.address, "0x")
    ).to.be.revertedWithCustomError(substrate, "AgentNotActive");
    await expect(
      substrate.connect(ownerW).revokeAgent(stranger.address, "0x")
    ).to.be.revertedWithCustomError(substrate, "AgentNotActive");
  });

  it("revoking a parent kills the whole subtree", async function () {
    await regTop(agentA);
    await registerAgent(substrate, agentB, "kid", ownerW, agentA.address, agentA);
    await registerAgent(substrate, agentC, "grand", ownerW, agentB.address, agentB);
    for (const a of [agentA, agentB, agentC]) {
      expect(await substrate.isLive(a.address)).to.equal(true);
    }
    await substrate.connect(ownerW).revokeAgent(agentA.address, "0x");
    for (const a of [agentA, agentB, agentC]) {
      expect(await substrate.isLive(a.address)).to.equal(false);
    }
    // descendants are blocked from acting and from escaping via rotation
    for (const a of [agentB, agentC]) {
      await expect(
        substrate.connect(a).registerTool(digest("m-" + a.address))
      ).to.be.revertedWithCustomError(substrate, "AgentNotActive");
      const sig = await signBinding(substrate, otherOwnerW, a.address, ZERO);
      await expect(
        substrate.connect(a).rotateOwner(otherOwnerW.address, ZERO, sig)
      ).to.be.revertedWithCustomError(substrate, "AgentNotActive");
      await expect(
        substrate.connect(a).updatePeerId(ethers.toUtf8Bytes("p"))
      ).to.be.revertedWithCustomError(substrate, "AgentNotActive");
    }
  });

  it("FleetTooDeep: a chain deeper than MAX_FLEET_DEPTH reverts", async function () {
    const max = Number(await substrate.MAX_FLEET_DEPTH());
    expect(max).to.equal(16);
    const wallets = [];
    for (let i = 0; i <= max + 1; i++) {
      const w = ethers.Wallet.createRandom().connect(ethers.provider);
      await ethers.provider.send("hardhat_setBalance", [w.address, "0x56BC75E2D63100000"]);
      wallets.push(w);
    }
    // depths 0..16 succeed
    await registerAgent(substrate, wallets[0], "d0", ownerW);
    for (let i = 1; i <= max; i++) {
      await registerAgent(substrate, wallets[i], "d" + i, ownerW, wallets[i - 1].address, wallets[i - 1]);
    }
    expect(await substrate.agentDepth(wallets[max].address)).to.equal(max);
    // depth 17 reverts
    await expect(
      registerAgent(substrate, wallets[max + 1], "d17", ownerW, wallets[max].address, wallets[max])
    ).to.be.revertedWithCustomError(substrate, "FleetTooDeep");
  });
});
