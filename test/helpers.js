const { ethers } = require("hardhat");

const ZERO = "0x0000000000000000000000000000000000000000";
const digest = (s) => ethers.keccak256(ethers.toUtf8Bytes(s));

// Reserved signer slots (tests use indices 0..15).
//   16: fee payer for seedPool, 17: fee recipient for seedPool,
//   18: default genesis anchor signer (registered agent),
//   19: default vaultMinter (test faucet for purchased ATN).
const SLOT_POOL_PAYER = 16;
const SLOT_POOL_RECIPIENT = 17;
const SLOT_GENESIS = 18;
const SLOT_VAULT = 19;

// substrate address (lowercase) -> { vaultFaucet, anchorSigners }
const _ctx = {};
const ctxOf = async (substrate) =>
  _ctx[(await substrate.getAddress()).toLowerCase()] || {};

// Deploy Substrate with explicit constructor args. Defaults: REP-less
// network (repToken = 0), quorum 5000 bps, genesis set = [signers[18]]
// with genesisQuorum 1 (the signer is registered as an agent here),
// vaultMinter = signers[19] (test faucet).
async function deploySubstrateWith(opts = {}) {
  const signers = await ethers.getSigners();
  const genesisSigner = signers[SLOT_GENESIS];
  const vaultSigner = signers[SLOT_VAULT];
  const treasury = opts.treasury !== undefined ? opts.treasury : signers[0].address;
  const vaultMinter =
    opts.vaultMinter !== undefined ? opts.vaultMinter : vaultSigner.address;
  const governor = opts.governor !== undefined ? opts.governor : ZERO;
  const repToken = opts.repToken !== undefined ? opts.repToken : ZERO;
  const quorumBps = opts.quorumBps !== undefined ? opts.quorumBps : 5000;
  const genesis = opts.genesisSigners || [genesisSigner];
  const genesisQuorum =
    opts.genesisQuorum !== undefined ? opts.genesisQuorum : genesis.length ? 1 : 0;
  const Substrate = await ethers.getContractFactory("Substrate");
  const substrate = await Substrate.deploy(
    treasury,
    vaultMinter,
    governor,
    repToken,
    quorumBps,
    genesis.map((g) => g.address),
    genesisQuorum
  );
  await substrate.waitForDeployment();
  if (opts.registerGenesis !== false) {
    for (let i = 0; i < genesis.length; i++) {
      await registerAgent(substrate, genesis[i], "genesis-signer-" + i);
    }
  }
  _ctx[(await substrate.getAddress()).toLowerCase()] = {
    vaultFaucet: vaultMinter === vaultSigner.address ? vaultSigner : null,
    anchorSigners: genesis.slice(0, Math.max(genesisQuorum, 1)),
  };
  return substrate;
}

async function deploySubstrate() {
  return deploySubstrateWith();
}

async function registerAgent(substrate, signer, lineage) {
  return substrate
    .connect(signer)
    .registerAgent(digest(lineage), ethers.toUtf8Bytes("peer-" + lineage));
}

async function deployToken(name = "TestUSD", symbol = "TUSD") {
  const Mock = await ethers.getContractFactory("MockERC20");
  const t = await Mock.deploy(name, symbol);
  await t.waitForDeployment();
  return t;
}

// ---------------------------------------------------------------------------
// Anchor authority (2026-10-12): EIP-712 AnchorApproval signatures.
// ---------------------------------------------------------------------------

const ANCHOR_TYPES = {
  AnchorApproval: [
    { name: "epochId", type: "string" },
    { name: "epochRoot", type: "bytes32" },
    { name: "prevEpochRoot", type: "bytes32" },
    { name: "prevAnchorHash", type: "bytes32" },
    { name: "agentMintCid", type: "string" },
    { name: "payloadHash", type: "bytes32" },
    { name: "agentMintRoot", type: "bytes32" },
  ],
};

async function anchorDomain(substrate) {
  const net = await ethers.provider.getNetwork();
  return {
    name: "AutonetSubstrate",
    version: "1",
    chainId: net.chainId,
    verifyingContract: await substrate.getAddress(),
  };
}

// Anchor fields chained onto the current chain head.
async function anchorFields(substrate, epochId, agentMintRoot) {
  return {
    epochId,
    epochRoot: digest("root-" + epochId),
    prevEpochRoot: await substrate.latestEpochRoot(),
    prevAnchorHash: await substrate.latestAnchorHash(),
    agentMintCid: "cid-" + epochId,
    payloadHash: digest("payload-" + epochId),
    agentMintRoot: agentMintRoot || ethers.ZeroHash,
  };
}

async function signAnchor(substrate, signer, f) {
  return signer.signTypedData(await anchorDomain(substrate), ANCHOR_TYPES, f);
}

// Signatures from `signers`, ordered by ascending signer address (the
// contract requires strictly ascending recovered addresses).
async function signAnchorSorted(substrate, signers, f) {
  const sorted = [...signers].sort((a, b) =>
    BigInt(a.address) < BigInt(b.address) ? -1 : 1
  );
  const sigs = [];
  for (const s of sorted) sigs.push(await signAnchor(substrate, s, f));
  return sigs;
}

function submitAnchorTx(substrate, relayer, f, sigs) {
  return substrate
    .connect(relayer)
    .submitAnchor(
      f.epochId,
      f.epochRoot,
      f.prevEpochRoot,
      f.prevAnchorHash,
      f.agentMintCid,
      f.payloadHash,
      f.agentMintRoot,
      sigs
    );
}

// Anchor `epochId` signed by `signers` (default: the deploy's genesis set).
async function anchorSigned(substrate, relayer, epochId, agentMintRoot, signers) {
  const f = await anchorFields(substrate, epochId, agentMintRoot);
  const ctx = await ctxOf(substrate);
  const sigs = await signAnchorSorted(substrate, signers || ctx.anchorSigners || [], f);
  await submitAnchorTx(substrate, relayer, f, sigs);
  return f;
}

// ---------------------------------------------------------------------------
// ATN faucet. Purchased-money rail: the test deploy wires vaultMinter to
// signers[19], so funding is a mintFromVault (no earnings ledger accrual).
// ---------------------------------------------------------------------------

function mintLeaf(agentAddr, amount) {
  const inner = ethers.keccak256(
    ethers.AbiCoder.defaultAbiCoder().encode(
      ["address", "uint256"],
      [agentAddr, amount]
    )
  );
  return ethers.keccak256(ethers.solidityPacked(["bytes32"], [inner]));
}

// Substrate's service fee (fee-recycled emission) is taken from every
// payForService at serviceFeeBps (default 2.5%). Callers wanting an exact net at the
// recipient gross-up: smallest g whose net (g - fee(g)) equals `net`.
function grossFor(net) {
  let g = (net * 10000n) / 9750n;
  while (g - (g * 250n) / 10000n < net) g += 1n;
  return g;
}

// Mint `amount` purchased ATN to `faucetSigner` (submitterSigner kept for
// call-site compatibility; unused).
async function mintATN(substrate, submitterSigner, faucetSigner, amount) {
  const ctx = await ctxOf(substrate);
  if (!ctx.vaultFaucet) throw new Error("mintATN: substrate has no test vault faucet");
  await substrate.connect(ctx.vaultFaucet).mintFromVault(faucetSigner.address, amount);
}

// Fund `to` with `amount` ATN by minting to the faucet and transferring.
async function fundATN(substrate, submitter, faucet, to, amount) {
  await mintATN(substrate, submitter, faucet, amount);
  await substrate.connect(faucet).transfer(to, amount);
}

// Burn real service fees, then anchor a seed epoch to close that fee
// window: the NEXT anchor's mintPool equals the burned amount (lag-1).
// Returns the pool the next anchor will carry.
let _seedCounter = 0;
async function seedPool(substrate, gross = 1_000_000_000n) {
  const signers = await ethers.getSigners();
  const payer = signers[SLOT_POOL_PAYER];
  const recipient = signers[SLOT_POOL_RECIPIENT];
  await mintATN(substrate, null, payer, gross);
  await substrate.connect(payer).payForService(recipient.address, gross, digest("seed"));
  _seedCounter += 1;
  await anchorSigned(substrate, payer, "seed-" + _seedCounter, ethers.ZeroHash);
  return substrate.nextMintPool();
}

// Earned ATN through the honest path: seed a pool, anchor a single-leaf
// epoch for `agentSigner`, claim it. Accrues agentMintTotal.
let _epochCounter = 0;
async function mintEarned(substrate, agentSigner, amount) {
  const pool = await seedPool(substrate, amount * 100n);
  if (pool < amount) throw new Error("mintEarned: pool too small");
  _epochCounter += 1;
  const epochId = "earn-" + _epochCounter;
  await anchorSigned(substrate, agentSigner, epochId, mintLeaf(agentSigner.address, amount));
  await substrate.connect(agentSigner).recordTrainingForEpoch(amount, digest(epochId), []);
  return epochId;
}

// Sign an EIP-712 voucher (channelId, cumulativeAmount) for a PaymentChannel.
async function signVoucher(channel, signer, channelId, cumulativeAmount) {
  const net = await ethers.provider.getNetwork();
  const domain = {
    name: "AutonetPaymentChannel",
    version: "1",
    chainId: net.chainId,
    verifyingContract: await channel.getAddress(),
  };
  const types = {
    Voucher: [
      { name: "channelId", type: "uint256" },
      { name: "cumulativeAmount", type: "uint256" },
    ],
  };
  return signer.signTypedData(domain, types, {
    channelId,
    cumulativeAmount,
  });
}

module.exports = {
  ZERO,
  digest,
  deploySubstrate,
  deploySubstrateWith,
  registerAgent,
  deployToken,
  signVoucher,
  mintLeaf,
  grossFor,
  mintATN,
  fundATN,
  seedPool,
  mintEarned,
  anchorFields,
  signAnchor,
  signAnchorSorted,
  submitAnchorTx,
  anchorSigned,
  ANCHOR_TYPES,
};
