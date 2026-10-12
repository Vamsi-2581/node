// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ISubstratePay {
    function payForService(address recipient, uint256 amount, bytes32 requestId) external returns (bool);
}

/// @dev Test-only: a CONTRACT payer (stand-in for the DAO's ToolBond, which
///      slashes by calling Substrate.payForService(accuser, stake, digest)
///      from contract context). Pins that payForService has no EOA-only or
///      agent-only payer check.
contract ServicePayerMock {
    function pay(address substrate, address recipient, uint256 amount, bytes32 requestId)
        external
        returns (bool)
    {
        return ISubstratePay(substrate).payForService(recipient, amount, requestId);
    }
}
