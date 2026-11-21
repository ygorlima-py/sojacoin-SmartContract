# SojaCoin

[**English**](README.md) | [Português](README.pt-BR.md)

A contract system for recording soybean deliveries and issuing SJC backed by audited inventory. **1 SJC represents one 60 kg bag**. The token has six decimal places to represent fractions of a bag.

Documents are stored off-chain; the contracts record their hashes and the steps in each operation. One deployment represents one company. Units identify where physical soybeans were received or will be withdrawn; SJC balances remain fungible within the company.

## Full local-network walkthrough

This walkthrough uses Anvil and the Solidity (Wake) extension for VS Code. It covers deployment through issuing SJC for a load. The addresses and hashes below are test examples.

### 1. Start the local network and compile

In a terminal, start Anvil:

```bash
~/.foundry/bin/anvil --host 127.0.0.1 --port 8545 --gas-price 0 --block-base-fee-per-gas 0
```

In Wake, connect to an existing network:

```text
RPC: http://127.0.0.1:8545
Chain ID: 31337
```

Click **Compile all** in Wake. Keep the Anvil terminal open during the test.

For each call below, open **Interact**, select the specified instance, expand the function, choose the account in **Sender Account**, enter the arguments in the order shown, and click **Call**. To deploy, select the contract under **Deploy Contracts** and enter constructor arguments, if any.

The default Anvil accounts are:

| Account | Use in this walkthrough | Address |
| --- | --- | --- |
| Account0 | Company administrator and contract deployer | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` |
| Account1 | Employee who receives and confirms loads at unit 1 | `0x70997970C51812dc3A010C7d01b50e0d17dc79C8` |
| Account2 | Auditor for unit 1 | `0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC` |
| Account3 | Auditor for unit 2 | `0x90F79bf6EB2c4f870365E785982E1f101E93b906` |
| Account4 | Producer | `0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65` |
| Account5 | Employee who creates batches and issues SJC for unit 1 | `0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc` |

These accounts' keys are public and are for local-network testing only. Never use them with real assets.

### 2. Deploy the modules and SojaCoin

In Wake, deploy these six logic modules. They take no arguments:

1. `SojaCoinAdministration`
2. `SojaCoinDeposits`
3. `SojaCoinCPR`
4. `SojaCoinServices`
5. `SojaCoinLocks`
6. `SojaCoinRedemptions`

Then deploy `SojaCoin` with these arguments, in order:

1. `initialOwner`: Account0
2. Address of the new `SojaCoinAdministration` deployment
3. Address of the new `SojaCoinDeposits` deployment
4. Address of the new `SojaCoinCPR` deployment
5. Address of the new `SojaCoinServices` deployment
6. Address of the new `SojaCoinLocks` deployment
7. Address of the new `SojaCoinRedemptions` deployment

In production, `initialOwner` can be the address of the company's Safe. SojaCoin
does not create or manage the Safe; it simply recognizes that address as the administrator.

Copy each address shown under **Deployed to** in Wake. Use modules built from the same compilation.

### 3. Deploy and connect the auxiliary contracts

After SojaCoin, deploy:

- `UnitRegistry(initialOwner, sojaCoinAddress)`: Account0 and the address of the newly deployed SojaCoin.
- `TokenIssuanceBatches(sojaCoinAddress)`: the same SojaCoin address.

In SojaCoin, with Account0 as the sender, call `configureUnitRegistry(unitRegistryAddress)` and `configureIssuanceBatches(issuanceBatchesAddress)`.

Check the links before continuing: `SojaCoin.unitRegistry()` should return the UnitRegistry in use; `SojaCoin.issuanceBatchModule()` should return the TokenIssuanceBatches in use; and `TokenIssuanceBatches.sojaCoin()` should return the current SojaCoin. Do not mix in addresses from earlier deployments.

### 4. Register units, auditors, and initial inventory

In `UnitRegistry`, with Account0, register two units. The hashes are test values:

```text
registerUnit(0x41524d415a454d2d303100000000000000000000000000000000000000000000, 0xfedef253c88d99aa89049b979b605d2526f7bd389d6993fb0956ba69a5a26ee1)
registerUnit(0x41524d415a454d2d303200000000000000000000000000000000000000000000, 0x2222222222222222222222222222222222222222222222222222222222222222)
```

The encoded codes represent `ARMAZEM-01` and `ARMAZEM-02`. The returned IDs should be `1` and `2`.

Assign the auditors with Account0:

```text
setUnitAuditor(1, 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC)
setUnitAuditor(2, 0x90F79bf6EB2c4f870365E785982E1f101E93b906)
```

Each auditor records only the unit assigned to them. Inventory amounts are whole kilograms; the hashes are test values:

```text
# sender: Account2
recordUnitAudit(1, 85000, 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
# sender: Account3
recordUnitAudit(2, 150000, 0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb)
```

In Wake's **Sender Account** field, select the account specified in each comment. For all other address arguments in this walkthrough, use the addresses from the table above.

Call `allUnitAuditsCurrent()`. It should return `true`. The aggregate inventory in this example is 235,000 kg, or `235000000000` mg.

Give Account1 only the permission to receive soybeans at unit 1. Then give
Account5 only the permission to issue SJC. The contract requires these to be different wallets for the same unit.

In UnitRegistry, with Account0, call:

```text
setPermissions(1, 0x70997970C51812dc3A010C7d01b50e0d17dc79C8, 1)
setPermissions(1, 0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc, 2)
```

The value `1` means receive soybeans, and `2` means issue SJC. Set separate
permissions for each unit where employees will operate.

### 5. Register the producer

In SojaCoin, with Account0 as the sender, propose Account4's registration. The hash
below is a test value:

```text
proposeParticipantRegistration(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 0x5555555555555555555555555555555555555555555555555555555555555555)
```

Set a future `deadline`, for example `2000000000`. Query this function on SojaCoin:

```text
participationTermsAcceptanceHash(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 2000000000)
```

Copy the returned digest. Account4 signs this digest in the Anvil terminal:

```bash
~/.foundry/bin/cast wallet sign --no-hash "PASTE_THE_DIGEST_HERE" --private-key "PASTE_ACCOUNT4_LOCAL_TEST_PRIVATE_KEY_HERE"
```

Finally, any account can submit the signature to SojaCoin:

```text
acceptParticipationTerms(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 2000000000, PASTE_ACCOUNT4_SIGNATURE_HERE)
```

In a real operation, this hash identifies the final agreement stored off-chain.
The signature proves that the participant's own wallet accepted the terms. The SaaS can
submit the final transaction without holding the producer's private key.

### 6. Open and confirm the load receipt

The truck arrives at unit 1. In SojaCoin, select Account1 and call this function. The third argument is the crop-year `bytes32` identifier; use the same value when registering CPRs for this crop year:

```text
openLoadReceipt(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65, 1, 0x43524f502d323032362f32303237000000000000000000000000000000000000)
```

The contract generates the deposit ID. After the transaction, get the value from the
`LoadReceiptOpened` event (or query `nextDepositId`) and use that ID in calls such as
`confirmLoadReceipt`, `linkTaxInvoice`, and `issueTokens`.
With a new deployment, the first ID in this walkthrough is `1`.

Confirm the weighing ticket with Account1. This example records 25,500 kg gross weight, 300 kg tare, and 450 kg in deductions. The weighing ticket timestamp must be later than the unit's audit and cannot be in the future. In the WSL terminal, run `date +%s` after the audits and use the returned number as `timestamp`. If the audit was recorded in the same second, wait one second and query again.

```text
confirmLoadReceipt(depositId=1, weighingTicket=SojaCoin.WeighingTicket(grossWeightKg=25500, taraKg=300, moistureBps=1550, impuritiesBps=0, damagedGrainBps=0, moistureDeductionKg=450, impuritiesDeductionKg=0, damagedGrainDeductionKg=0, otherDeductionsKg=0, weighingTicketHash=0x3333333333333333333333333333333333333333333333333333333333333333, reportHash=0x4444444444444444444444444444444444444444444444444444444444444444, timestamp=PASTE_RESULT_OF_date_+%s_HERE))
```

The net weight is 24,750 kg. Since the weighing ticket is later than the audit, unit 1 increases from 85,000 kg to 109,750 kg, and the company's aggregate inventory increases from 235,000 kg to 259,750 kg.

After the tax invoice is available, link it to the load with Account1. The hash below is a test value:

```text
linkTaxInvoice(1, 0x1111111111111111111111111111111111111111111111111111111111111111)
```

The invoice can be linked before or after the weighing ticket, but it must be linked before the batch is created.

### 7. Create and approve the batch

In `TokenIssuanceBatches`, with Account5, group deposit 1. The deadline must be a future Unix timestamp relative to the current block; `2000000000` works for this simulation as long as it is still in the future:

```text
createIssuanceBatch([1], 2000000000)
```

The contract generates the batch ID. After the transaction, get the value from the `BatchCreated`
event (or query `nextBatchId`) and use that ID in `issuanceBatchAuthorizationHash` and
`approveIssuanceBatch`.
With a new batch contract, the first ID in this walkthrough is also `1`.

Query `issuanceBatchAuthorizationHash(1)` on TokenIssuanceBatches and copy the digest. The producer, Account4, signs it using the local test private key printed in the Anvil terminal:

```bash
~/.foundry/bin/cast wallet sign --no-hash "PASTE_THE_DIGEST_HERE" --private-key "PASTE_ACCOUNT4_LOCAL_TEST_PRIVATE_KEY_HERE"
```

Submit the returned signature with `approveIssuanceBatch(1, signature)` on TokenIssuanceBatches. The transaction can be submitted by Account0, Account1, or another wallet; the signature must come from the producer. In the SaaS, the producer signs once and the application can submit this blockchain transaction afterward.

### 8. Issue and check the SJC

After approval, in SojaCoin, with Account5 as the sender, call:

```text
issueTokens(1)
```

With 24,750 kg net weight and no CPR registered, the producer receives 412.5 SJC. Since `balanceOf` returns integer units and `decimals()` is 6, the result is `412500000`, equivalent to 412.5 SJC. Query:

```text
balanceOf(0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65)
```

If the producer has open CPRs for the same producer, unit, and crop year, issuance repays those debts in the approved order. The SJC for each repayment goes directly to the CPR's current creditor. Only the amount remaining after the compatible queue is paid goes to the producer. Example: with a debt of 7,000 bags and an 8,000-bag delivery, 7,000 SJC goes to the creditor, 1,000 SJC goes to the producer, and the CPR is paid off.

CPR amounts use the token's internal units. Since `decimals()` is 6, 7,000 SJC is entered as `7000000000` units. The contract reduces the outstanding balance with each issuance; it does not hold CPR SJC in escrow.

## Automated tests

With Foundry installed, run the CPR tests from the project root:

```bash
forge test --match-path test/SojaCoinCPR.t.sol
```

The tests cover full and partial repayments, CPR queues, compatibility by unit and crop year, authorization to use another unit, priority acceptance, assignment, late repayment, cancellation, agreed closure, and duplicate weighing tickets.

## Notes

- All hashes filled with `1111`, `2222`, `3333`, `4444`, `5555`, `aaaa`, or `bbbb` in this walkthrough are test values. In a real operation, the application calculates the hash of the file stored off-chain.
- Each active unit must have an assigned auditor and a current audit to enable issuance.
- The batch must be created in the same `TokenIssuanceBatches` contract connected to the current SojaCoin. If `createIssuanceBatch` reverts, check the contract address getters from step 3.
- Module addresses are fixed in SojaCoin, and auxiliary contracts point to a specific deployment. After changing the code, deploy a new version and repeat the registrations on that deployment.

More details are available in [the architecture document](docs/arquitetura.md) and [the soybean delivery logic](LOGICA_ENTREGA_SOJA.md).
