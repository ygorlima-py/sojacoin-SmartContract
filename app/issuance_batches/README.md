# Issuance batches

This app creates a batch from a blockchain-confirmed receipt and tracks its
approval and token issuance. The operator creates the batch; the named
participant approves it from their own wallet. For the current one-receipt MVP,
that approval and the SJC issuance happen in one atomic blockchain transaction.

## Emissions dashboard

Open **Emissões** in the company sidebar to review issuance batches and eligible
receipts. Access requires the active company **Issuance** role and active
issuance access to at least one unit. The page only lists records for that
company and those units.

Use the search field for a participant name or on-chain batch number, then
filter batches by unit or status. Each table has its own pagination. A receipt
appears under **Recebimentos disponíveis** only after blockchain confirmation,
with positive net weight, and while no prepared, pending, completed, or review
batch reserves it. A confirmed cancellation releases the receipt.

**Abrir** on a batch or receipt returns to its receipt detail page and the
existing batch controls. Batch creation, approval links, confirmation recovery,
and cancellation remain in that flow; the dashboard does not add a second
issuance action. An expired approval deadline does not release a receipt until
the batch cancellation is confirmed on chain.

## Flow in the SaaS

1. A staff member with the company's issuance role opens **Recebimentos** and
   selects a receipt whose blockchain registration is confirmed.
2. The staff member creates a batch with an approval deadline. The connected
   verified wallet must have active issuance permission for the unit. The
   contract also checks the audit, inventory backing and receipt state.
3. Once the creation transaction is confirmed, the staff member creates and
   copies the private approval URL to the participant.
4. The participant opens the URL, checks the receipt and attached PDFs, connects
   the wallet saved on the participant record, and chooses
   **Aprovar e receber tokens**.
5. MetaMask asks for one transaction to `approveAndIssueBatch(batchId)` on
   `TokenIssuanceBatches`. The participant's wallet sends it and pays its network
   fee. The SaaS confirms the receipt, events from both contracts, and final
   on-chain state before showing the issuance as complete.

The participant does not sign a separate EIP-712 message in this SaaS flow. The
contract's existing `approveIssuanceBatch(batchId, signature)` entry point is
retained for compatibility, but the new page does not call it. A reverted
issuance reverts the approval in the same transaction, so the batch remains
pending and no partial issuance is recorded.

## Authorization and issuance rules

The participant wallet must match the batch's recorded participant. The batch
must still be pending, within its deadline, and contain exactly one receipt.
The batch contract marks it approved before calling SojaCoin because issuance
requires an approved batch. Solidity's transaction atomicity rolls back that
state and event if any issuance check fails.

The batch creator remains the responsible operator. SojaCoin obtains that
address from the on-chain batch record and rechecks its current UnitRegistry
issuance permission. The participant cannot select or impersonate the operator.
The shared issuance routine keeps the regular checks and behavior for audit
validity, available inventory, receipt status, physical returns, token rounding,
CPR repayments, inventory closure, and `TokensIssued`.

The SaaS requires all of these events in the same transaction receipt:

- `BatchApproved`, `BatchIssuanceRecorded` and `BatchCompleted` from
  `TokenIssuanceBatches`.
- `TokensIssued` from `SojaCoin`.

It also reads the batch, deposit and issuance state at the receipt block. An
event emitted by one contract is stored against that contract's deployment
record. A missing or inconsistent event/state moves the batch to review. A
reverted transaction remains in history and can be retried; an uncertain send
must be recovered with the same transaction hash rather than resent
automatically.

## Issuance amounts and balance

The confirmation stores integer token units and the `decimals()` value from the
receipt block. The current SojaCoin has six decimals. The participant page and
receipt page separately show:

- Total SJC minted for the receipt.
- The portion minted to the participant. CPR repayments may send part of the
  issuance directly to creditors.
- The participant's current `balanceOf` result and the latest block queried.
- The SojaCoin contract address, confirming transaction hash and block number.

Formatting uses decimal integer arithmetic; it does not convert token values
through binary floating point. A balance query can be temporarily unavailable
without hiding the confirmed issuance record.

## Links, expiration and recovery

The private approval URL has a seven-day lifetime and can be canceled by the
company. The database stores only a digest of the random URL token. A consumed
or canceled URL remains in history. The on-chain approval deadline is separate:
when it expires, the receipt remains reserved until an authorized cancellation
transaction is confirmed.

Before MetaMask is opened, the exact transaction data and a prepared transaction
record are saved. Explicit wallet rejection closes that attempt and allows a
new one while the batch remains valid. If the wallet may have broadcast but did
not return a hash, the participant can recover the existing attempt. The SaaS
never stores private keys or sends a replacement transaction automatically.

## Deployment and migration

The new behavior requires freshly deployed contracts. `SojaCoin`, its
`SojaCoinDeposits` delegate module, and `TokenIssuanceBatches` must be compiled
from the same source version and linked to one another. Existing deployments
cannot gain the new function by updating Django artifacts; deployed bytecode is
immutable. Create a new deployment through the SaaS deployment workflow and
register units, permissions, audits, participants, receipts and batches on that
network again. Existing Django accounts, uploaded PDFs and blockchain history
remain in the database; do not delete them as part of deployment.

After updating the code, apply the migration from `app`:

```bash
uv run python manage.py migrate
```

The generated Django artifacts are the ABI and creation bytecode files under
`blockchain/abis/` and `blockchain/bytecode/`. They must match the contracts
compiled from `contracts/src/contracts/` before a new deployment is started.

## Checks

From `contracts`:

```bash
forge test
```

From `app`:

```bash
uv run python manage.py test blockchain issuance_batches receipts participants
```

The tests cover single-transaction approval and issuance, permission rechecks,
atomic rollback, CPR distribution, transaction recovery and event attribution
across both contracts. A live MetaMask demonstration still needs an accessible
RPC endpoint and separate operator and participant wallets. A local Anvil
endpoint at `127.0.0.1` is reachable only from the machine running Anvil.
