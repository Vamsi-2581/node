const { expect } = require("chai");
const { ethers } = require("hardhat");
const {
  ZERO,
  digest,
  registerAgent,
  mintLeaf,
  mintATN,
  deploySubstrateWith,
  seedPool,
  anchorFields,
  signAnchor,
  signAnchorSorted,
  submitAnchorTx,
} = require("./helpers");

// Anchor authority + on-chain conservation (2026-10-12).
//
// Attack being closed: before this change submitAnchor had no caller check
// and recordTrainingForEpoch only checked the proof against whatever root was
// anchored. Anyone could anchor a fake epoch paying themselves, mint
// unlimited ATN, claim REP 1:1 DAO-side, and take over the DAO.

const byAddr = (a, b) => (BigInt(a.address) < BigInt(b.address) ? -1 : 1);

async function increaseTime(sec) {
  await ethers.provider.send("evm_increaseTime", [sec]);
  await ethers.provider.send("evm_mine", []);
}

describe("Substrate anchor authority + conservation", function () {
  let signers, attacker, relayer, a1, a2, a3, payer, provider;

  beforeEach(async function () {
    signers = await ethers.getSigners();
    [, attacker, relayer, a1, a2, a3, payer, provider] = signers;
  });

  describe("constructor validation", function () {
    it("rejects quorum bps 0 and >= 10000", async function () {
      const F = await ethers.getContractFactory("Substrate");
      await expect(
        F.deploy(ZERO, ZERO, ZERO, ZERO, 0, [a1.address], 1)
      ).to.be.revertedWithCustomError(F, "BadAnchorQuorum");
      await expect(
        F.deploy(ZERO, ZERO, ZERO, ZERO, 10000, [a1.address], 1)
      ).to.be.revertedWithCustomError(F, "BadAnchorQuorum");
    });

    it("rejects REP-less deploy without a genesis quorum", async function () {
      const F = await ethers.getContractFactory("Substrate");
      await expect(
        F.deploy(ZERO, ZERO, ZERO, ZERO, 5000, [], 0)
      ).to.be.revertedWithCustomError(F, "NoAnchorAuthority");
    });

    it("rejects duplicate genesis signers and quorum > set size", async function () {
      const F = await ethers.getContractFactory("Substrate");
      await expect(
        F.deploy(ZERO, ZERO, ZERO, ZERO, 5000, [a1.address, a1.address], 1)
      ).to.be.revertedWithCustomError(F, "BadGenesisSigner");
      await expect(
        F.deploy(ZERO, ZERO, ZERO, ZERO, 5000, [a1.address], 2)
      ).to.be.revertedWithCustomError(F, "BadGenesisQuorum");
    });
  });

  describe("genesis bootstrap (no REP)", function () {
    let substrate;
    beforeEach(async function () {
      substrate = await deploySubstrateWith({
        genesisSigners: [a1, a2, a3],
        genesisQuorum: 2,
      });
    });

    it("2-of-3 genesis signers anchor; relayer identity is irrelevant", async function () {
      const f = await anchorFields(substrate, "g1");
      const sigs = await signAnchorSorted(substrate, [a1, a3], f);
      await expect(submitAnchorTx(substrate, relayer, f, sigs))
        .to.emit(substrate, "AnchorAuthorized")
        .withArgs(digest("g1"), 2, 2, 2, true, 0, 0);
      expect(await substrate.anchorCount()).to.equal(1n);
    });

    it("1-of-3 genesis signers is insufficient", async function () {
      const f = await anchorFields(substrate, "g1");
      const sigs = await signAnchorSorted(substrate, [a2], f);
      await expect(submitAnchorTx(substrate, relayer, f, sigs))
        .to.be.revertedWithCustomError(substrate, "AnchorQuorumNotMet")
        .withArgs(1, 2, true);
    });

    it("outsider anchor reverts: no sigs, self-signed, and registered non-genesis", async function () {
      await registerAgent(substrate, attacker, "attacker");
      const f = await anchorFields(substrate, "evil", mintLeaf(attacker.address, 10n ** 18n));
      await expect(submitAnchorTx(substrate, attacker, f, [])).to.be.revertedWithCustomError(
        substrate,
        "AnchorQuorumNotMet"
      );
      const own = await signAnchor(substrate, attacker, f);
      await expect(submitAnchorTx(substrate, attacker, f, [own])).to.be.revertedWithCustomError(
        substrate,
        "AnchorQuorumNotMet"
      );
    });

    it("signatures over different content do not authorize", async function () {
      const f = await anchorFields(substrate, "g1");
      const sigs = await signAnchorSorted(substrate, [a1, a2], f);
      const tampered = { ...f, agentMintRoot: mintLeaf(attacker.address, 1n) };
      // Recovered addresses are garbage -> either unsorted or zero weight.
      await expect(submitAnchorTx(substrate, attacker, tampered, sigs)).to.be.reverted;
    });

    it("duplicate / unsorted signers revert", async function () {
      const f = await anchorFields(substrate, "g1");
      const [lo, hi] = [a1, a2].sort(byAddr);
      const sLo = await signAnchor(substrate, lo, f);
      const sHi = await signAnchor(substrate, hi, f);
      await expect(submitAnchorTx(substrate, relayer, f, [sLo, sLo])).to.be.revertedWithCustomError(
        substrate,
        "SignersNotSorted"
      );
      await expect(submitAnchorTx(substrate, relayer, f, [sHi, sLo])).to.be.revertedWithCustomError(
        substrate,
        "SignersNotSorted"
      );
    });

    it("unregistered genesis signer carries no weight", async function () {
      const sub2 = await deploySubstrateWith({
        genesisSigners: [a1],
        genesisQuorum: 1,
        registerGenesis: false,
      });
      const f = await anchorFields(sub2, "g1");
      const sigs = await signAnchorSorted(sub2, [a1], f);
      await expect(submitAnchorTx(sub2, relayer, f, sigs)).to.be.revertedWithCustomError(
        sub2,
        "AnchorQuorumNotMet"
      );
    });
  });

  describe("REP-weighted quorum", function () {
    let substrate, rep;
    beforeEach(async function () {
      const R = await ethers.getContractFactory("MockRepToken");
      rep = await R.deploy();
      await rep.waitForDeployment();
      // REP exists before deploy: a1 40, a2 30, a3 20, attacker 10 (of 100).
      await rep.setVotes(a1.address, 40n);
      await rep.setVotes(a2.address, 30n);
      await rep.setVotes(a3.address, 20n);
      await rep.setVotes(attacker.address, 10n);
      await increaseTime(5);
      substrate = await deploySubstrateWith({
        repToken: await rep.getAddress(),
        quorumBps: 5000,
        genesisSigners: [],
        genesisQuorum: 0,
      });
      for (const [s, l] of [[a1, "a1"], [a2, "a2"], [a3, "a3"], [attacker, "atk"]]) {
        await registerAgent(substrate, s, l);
      }
    });

    it("signers holding > 50% of past supply anchor", async function () {
      const f = await anchorFields(substrate, "r1");
      const sigs = await signAnchorSorted(substrate, [a1, a3], f); // 60
      await expect(submitAnchorTx(substrate, relayer, f, sigs))
        .to.emit(substrate, "AnchorAuthorized")
        .withArgs(digest("r1"), 2, 60, 100, false, 0, 0);
    });

    it("exactly 50% is insufficient (strict majority), as is the attacker", async function () {
      const f = await anchorFields(substrate, "r1");
      let sigs = await signAnchorSorted(substrate, [a2, a3], f); // 50
      await expect(submitAnchorTx(substrate, relayer, f, sigs))
        .to.be.revertedWithCustomError(substrate, "AnchorQuorumNotMet")
        .withArgs(50, 100, false);
      sigs = await signAnchorSorted(substrate, [attacker], f);
      await expect(submitAnchorTx(substrate, attacker, f, sigs)).to.be.revertedWithCustomError(
        substrate,
        "AnchorQuorumNotMet"
      );
    });

    it("weights are read at the PREVIOUS anchor's timestamp, not now", async function () {
      let f = await anchorFields(substrate, "r1");
      await submitAnchorTx(substrate, relayer, f, await signAnchorSorted(substrate, [a1, a2], f));
      await increaseTime(5);
      // Attacker inflates votes AFTER the snapshot: no effect on next anchor.
      await rep.setVotes(attacker.address, 1000n);
      f = await anchorFields(substrate, "r2");
      await expect(
        submitAnchorTx(substrate, attacker, f, await signAnchorSorted(substrate, [attacker], f))
      ).to.be.revertedWithCustomError(substrate, "AnchorQuorumNotMet");
      // Honest majority still works at the old snapshot.
      await submitAnchorTx(substrate, relayer, f, await signAnchorSorted(substrate, [a1, a2], f));
      expect(await substrate.anchorCount()).to.equal(2n);
    });

    it("undelegated supply counts in the denominator", async function () {
      await rep.addUndelegatedSupply(100n); // supply 200, votes 100
      await increaseTime(5);
      const sub2 = await deploySubstrateWith({
        repToken: await rep.getAddress(),
        genesisSigners: [],
        genesisQuorum: 0,
      });
      await registerAgent(sub2, a1, "a1");
      await registerAgent(sub2, a2, "a2");
      await registerAgent(sub2, a3, "a3");
      const f = await anchorFields(sub2, "r1");
      await expect(
        submitAnchorTx(sub2, relayer, f, await signAnchorSorted(sub2, [a1, a2, a3], f))
      )
        .to.be.revertedWithCustomError(sub2, "AnchorQuorumNotMet")
        .withArgs(90, 200, false);
    });

    it("genesis set is powerless once REP exists", async function () {
      const sub2 = await deploySubstrateWith({
        repToken: await rep.getAddress(),
        genesisSigners: [provider],
        genesisQuorum: 1,
      });
      const f = await anchorFields(sub2, "r1");
      await expect(
        submitAnchorTx(sub2, relayer, f, await signAnchorSorted(sub2, [provider], f))
      )
        .to.be.revertedWithCustomError(sub2, "AnchorQuorumNotMet")
        .withArgs(0, 100, false);
    });

    it("anchorQuorum view mirrors the submit check", async function () {
      const addrs = [a1, a2].sort(byAddr).map((s) => s.address);
      const [w, t, g, ok] = await substrate.anchorQuorum(addrs);
      expect([w, t, g, ok]).to.deep.equal([70n, 100n, false, true]);
    });
  });

  describe("on-chain conservation (fees-only pool)", function () {
    let substrate, gen;
    beforeEach(async function () {
      gen = signers[18];
      substrate = await deploySubstrateWith();
      await registerAgent(substrate, a1, "a1");
      await registerAgent(substrate, a2, "a2");
    });

    it("first anchor has a zero pool: any mint reverts", async function () {
      const f = await anchorFields(substrate, "e1", mintLeaf(a1.address, 1n));
      await submitAnchorTx(substrate, relayer, f, await signAnchorSorted(substrate, [gen], f));
      await expect(
        substrate.connect(a1).recordTrainingForEpoch(1n, digest("e1"), [])
      ).to.be.revertedWithCustomError(substrate, "EpochPoolExceeded");
    });

    it("pool = previous window's burned fees; mint over pool reverts", async function () {
      // 1_000_000 gross, fee 2.5% = 25_000, half to treasury -> 12_500 burned.
      await mintATN(substrate, null, payer, 1_000_000n);
      await substrate.connect(payer).payForService(provider.address, 1_000_000n, digest("x"));
      expect(await substrate.burnedSinceAnchor()).to.equal(12_500n);
      let f = await anchorFields(substrate, "e1");
      await submitAnchorTx(substrate, relayer, f, await signAnchorSorted(substrate, [gen], f));
      expect(await substrate.burnedSinceAnchor()).to.equal(0n);
      expect(await substrate.nextMintPool()).to.equal(12_500n);

      // Signed root over-allocates (12_000 + 600 > 12_500): the late claim reverts.
      const l1 = mintLeaf(a1.address, 12_000n);
      const l2 = mintLeaf(a2.address, 600n);
      const [lo, hi] = l1 <= l2 ? [l1, l2] : [l2, l1];
      const root = ethers.keccak256(ethers.solidityPacked(["bytes32", "bytes32"], [lo, hi]));
      f = await anchorFields(substrate, "e2", root);
      await submitAnchorTx(substrate, relayer, f, await signAnchorSorted(substrate, [gen], f));
      const anchor = await substrate.getAnchorByEpochId("e2");
      expect(anchor.mintPool).to.equal(12_500n);

      await substrate.connect(a1).recordTrainingForEpoch(12_000n, digest("e2"), [l2]);
      await expect(substrate.connect(a2).recordTrainingForEpoch(600n, digest("e2"), [l1]))
        .to.be.revertedWithCustomError(substrate, "EpochPoolExceeded")
        .withArgs(digest("e2"), 12_500n, 12_600n);
      expect((await substrate.getAnchorByEpochId("e2")).minted).to.equal(12_000n);
    });

    it("honest path: pool-exact mints all claim", async function () {
      const pool = await seedPool(substrate);
      const half = pool / 2n;
      const rest = pool - half;
      const l1 = mintLeaf(a1.address, half);
      const l2 = mintLeaf(a2.address, rest);
      const [lo, hi] = l1 <= l2 ? [l1, l2] : [l2, l1];
      const root = ethers.keccak256(ethers.solidityPacked(["bytes32", "bytes32"], [lo, hi]));
      const f = await anchorFields(substrate, "h1", root);
      await submitAnchorTx(substrate, relayer, f, await signAnchorSorted(substrate, [gen], f));
      await substrate.connect(a1).recordTrainingForEpoch(half, digest("h1"), [l2]);
      await substrate.connect(a2).recordTrainingForEpoch(rest, digest("h1"), [l1]);
      expect(await substrate.networkMintTotal()).to.equal(pool);
    });

    it("a contract can call payForService (ToolBond slashing path) and its fee feeds the pool", async function () {
      const M = await ethers.getContractFactory("ServicePayerMock");
      const payerContract = await M.deploy();
      await payerContract.waitForDeployment();
      const pcAddr = await payerContract.getAddress();
      await substrate.connect(signers[19]).mintFromVault(pcAddr, 400_000n);
      await expect(
        payerContract.pay(await substrate.getAddress(), provider.address, 400_000n, digest("slash"))
      ).to.emit(substrate, "ServicePayment");
      // fee 10_000; 5_000 to treasury, 5_000 burned.
      expect(await substrate.balanceOf(provider.address)).to.equal(390_000n);
      expect(await substrate.serviceEarnings(provider.address)).to.equal(390_000n);
      expect(await substrate.burnedSinceAnchor()).to.equal(5_000n);
    });
  });
});
