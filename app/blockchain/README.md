# Blockchain app

The `blockchain` app stores the local reconciliation state between Django and
the deployed SojaCoin contracts. It does not store private keys and it does
not treat a submitted transaction as completed before a confirmation is
observed on the configured network.

## Implemented models

| Model | Responsibility |
| --- | --- |
| `Network` | Stores the chain ID, a protected RPC setting reference and whether the network can receive new work. Networks are deactivated instead of deleted. |
| `ContractInstance` | Stores one company's deployed contract address, contract kind and version on a network. Addresses are normalized and unique per network. |
| `BlockchainTransaction` | Tracks a transaction from `prepared` to `submitted`, `confirmed`, `failed` or `reorged`. The record cannot be deleted. |
| `ProcessedEvent` | Marks a contract log as applied. The network, transaction hash and log index are unique, and the record is append-only. |
| `DeploymentProcess` | Tracks the company, network, public owner address and progress of the nine contract deployments. It can require review after a deployment transaction is reorganized. |
| `DeploymentStep` | Records one contract in deployment order and links its confirmed contract instance. |
| `DeploymentAttempt` | Links a step to each transaction attempt, preserving the exact unsigned creation data, the wallet-send start time and whether the step still has an open attempt. |
| `DeploymentConfigurationAttempt` | Preserves each attempt to link Unit Registry or Issuance Batches to SojaCoin, including the expected address and exact ABI call data. Its linked `BlockchainTransaction` stores the sender, hash, receipt state and timestamps. |
| `ParticipantRegistrationAttempt` | Links a producer proposal or terms acceptance to its prepared transaction, exact call data and wallet or public-link recovery context. The model belongs to `participants`. |

Every model has `created_at` and `updated_at`. A transaction is complete only
when it has status `confirmed`, block data and a confirmation timestamp.

## Services

### `blockchain.services.deployment`

`start_deployment_process(...)` creates the local process and nine ordered
steps in one database transaction. It refuses to start when one of the nine
contract kinds is already active for the selected company and network, or when
another process is in progress or needs review. The platform caller must
authorize the operator. It does not sign or send transactions. `DEPLOYED` means
that all nine linked contracts still have matching confirmed deployment
transactions; it does not mean that the two owner calls have been performed.

`cancel_deployment_process(process_id)` cancels an in-progress process only
while none of its deployment attempts has a transaction hash, submission time
or unresolved wallet-send request. Prepared attempts without a wallet-send
request, and attempts with an explicit wallet rejection, are kept in history.
A process with a submitted, confirmed, failed-after-submission or reorganized
transaction cannot be canceled. A canceled process remains in the database,
and a new process can be started for the same company and network.

When `mark_transaction_reorged(...)` handles a transaction linked to a
deployment attempt, it moves the process to `NEEDS_REVIEW` in the same database
transaction. The step is no longer counted as confirmed, and the Admin shows
the reverted confirmation. Contract and transaction history remain available;
the service does not redeploy automatically. Reorganization detection on the
RPC is separate work and must call this lifecycle service when detected.

`prepare_deployment_step(step_id)` builds unsigned creation data for one saved
deployment step. It reads the company, network and owner wallet from the step's
process, requires the process and its company and network to be active, and
requires all earlier steps to have matching confirmed deployment transactions.
Constructor dependencies are read from confirmed, active contracts linked to
steps in that same process. SojaCoin uses the six module addresses. Unit Registry
and Issuance Batches use the confirmed SojaCoin address.

`prepare_deployment_data(...)` receives those verified dependency addresses and
builds the constructor data using the packaged ABI and bytecode. A preparation
error leaves the process, its steps and its attempts unchanged. These functions
do not create a transaction record, sign, send or confirm a deployment.

`start_deployment_attempt(step_id, sender_address, wallet_chain_id)` calls the
preparation service, then creates a `PREPARED` transaction and its
`DeploymentAttempt` together. The attempt stores the exact contract creation
data so the server can later compare it to the transaction observed on the RPC.
An open-attempt database constraint plus a locked step prevents two operators
from starting separate attempts for the same step. Repeating preparation for
the same wallet before opening MetaMask returns the existing attempt.

`mark_deployment_submission_started(attempt_id)` records that the wallet prompt
is about to open. It is a one-time marker: a repeated request does not reopen
the send action. An explicit MetaMask rejection calls
`fail_deployment_attempt(attempt_id)`, which records a failed transaction and
releases that step for a new attempt. Other wallet errors remain unresolved and
keep the attempt open, so the process cannot be canceled or sent again by
mistake.

`register_deployment_transaction_hash(attempt_id, transaction_hash)` looks up
the wallet hash on the process's saved network. It checks the RPC chain, sender,
contract-creation destination and exact prepared data before calling
`submit_transaction(...)`. A hash not found yet returns `None`; an RPC or local
write failure leaves the same attempt available for the operator to retry.
Repeating an already recorded hash is idempotent. The browser keeps a returned
hash in the current tab session so it can be presented again if the server
response is lost. No private key is sent to Django.

The Django Admin provides the assisted start and review screens at
`/admin/blockchain/deploymentprocess/`. An authorized staff member selects an
active company, an active network and the public wallet that will own the
company's SojaCoin and unit registry. The service creates the process and its
nine ordered steps. The detail page shows the latest known status, transaction
hash and contract address for each step.
Starting a process does not call the RPC, sign a transaction or deploy a
contract. The standard Admin add route also opens this validated start form.
The detail page has a POST-only, CSRF-protected cancel action while the process
is in progress. Processes and steps cannot be edited or deleted there.
For the currently available step, an authorized Admin operator can connect
MetaMask, create and submit the contract transaction, and send its hash back for
RPC verification. The custom POST routes require the Admin add permission and
are protected by CSRF. They scope every step and attempt to the process ID in
the URL.

### `blockchain.services.deployment_confirmation`

`confirm_deployment_attempt(attempt_id)` checks the saved deployment
transaction against the process network. It requires the RPC transaction to
match the saved sender, chain ID and prepared creation data, then checks the
receipt and code at the resulting contract address. It records the confirmed
transaction, `ContractInstance`, and `DeploymentStep` link in one database
transaction. Repeating a successful confirmation returns the existing contract.
Only the ninth confirmed step changes the process to `DEPLOYED`.

The Admin action **Consultar confirmação** calls this service for the current
attempt. It only reads the existing transaction; it does not open MetaMask or
send another transaction. A pending receipt or RPC error leaves the same
attempt available for another query.

### `blockchain.services.deployment_configuration`

After the nine contracts are confirmed, this service prepares and tracks the
two owner-only calls on SojaCoin:

| Configuration | SojaCoin call | Address passed |
| --- | --- | --- |
| Unit Registry | `configureUnitRegistry(address)` | Confirmed Unit Registry from the same process |
| Issuance Batches | `configureIssuanceBatches(address)` | Confirmed Issuance Batches from the same process |

`prepare_deployment_configuration(...)` reads the target from the process steps,
checks that the connected public wallet is the current SojaCoin owner, checks
the expected network and existing on-chain links, and encodes the call with the
packaged ABI. It stores one open `DeploymentConfigurationAttempt` before the
browser opens the wallet. The browser gets only the public sender, target,
chain ID and encoded call; Django never receives a private key.

`mark_configuration_submission_started(attempt_id)` records that the wallet
prompt is about to open. `reject_configuration_attempt(attempt_id)` closes the
attempt only after an explicit wallet rejection and cannot mark an already
submitted hash as failed. An uncertain send stays open so the same hash can be
retried.

`register_configuration_transaction_hash(...)` checks the hash through the
saved network and compares the transaction sender, destination, chain ID, zero
value and exact prepared call before moving the transaction to `submitted`.
`confirm_deployment_configuration(attempt_id)` checks the receipt, expected
SojaCoin event and getter value at the receipt block. The transaction
confirmation and attempt closure are committed together. A mismatch preserves
the evidence and moves the process to `NEEDS_REVIEW`; a repeated successful
query returns the existing result.

The Admin page shows two distinct outcomes: **Nove contratos: 9 de 9
confirmados** and **Vínculos: configuração concluída**. The owner wallet signs
nine separate contract deployments and two separate configuration calls: eleven
wallet approvals in total. The process remains labeled `Contratos implantados`;
the UI reports the two link confirmations separately.

### `blockchain.services.contract_registration`

`register_contract(company=..., network=..., kind=..., address=..., version=...)`
registers one contract address in the local database. It requires a saved,
active company and network; the model validates the address, contract kind,
version and uniqueness. The caller must authorize the user. The optional
`deployment_transaction_hash` is saved when provided.

Registration does not contact the RPC or prove that the address contains the
expected contract. `active` indicates a locally active record, not blockchain
verification.

### `blockchain.services.contract_verification`

`verify_company_contracts(...)` reads the company's nine active registered
addresses from the configured network. It checks for deployed code, compares
the six module addresses and the links between the three main contracts, and
compares both owners with addresses supplied by the authorized caller. All
reads use the same block number. The result contains a list of individual
checks and an overall `passed` property; it does not change database records
or send transactions. A successful result checks these connections, but does
not authenticate the bytecode or prove the contracts are safe.

### `blockchain.services.rpc`

`connect_to_rpc(network)` creates a read-capable Web3 connection using
`Network.rpc_url` and verifies the response against that same record's
`chain_id`. Each saved network can therefore use its own RPC endpoint. It does
not hold a private key or send a transaction. RPC URLs are managed in Django
Admin by superusers because some providers include credentials in the URL.

### `blockchain.services.contracts`

- `load_contract_abi(kind)` loads the ABI packaged in `blockchain/abis` for a
  supported contract kind.
- `bind_contract_to_web3(web3, deployment)` binds an address and ABI to an
  existing RPC connection. It does not enforce the deployment's active flag;
  callers apply the rules required by their operation.
- `get_web3_contract(instance)` binds that ABI to the active contract address
  stored in `ContractInstance`. It rejects inactive deployments and networks,
  and checks the RPC against the deployment's saved chain ID.

Example read call:

```python
from blockchain.services.contracts import get_web3_contract

contract = get_web3_contract(unit_registry_instance)
active_unit_count = contract.functions.activeUnitCount().call()
```

The ABI files are copied from the corresponding Foundry artifacts and included
with the Django app. This avoids relying on the generated `contracts/out`
directory at runtime. Supported contract kinds are `SojaCoin`, `UnitRegistry`,
`TokenIssuanceBatches`, and the six SojaCoin logic modules:
`SojaCoinAdministration`, `SojaCoinDeposits`, `SojaCoinCPR`,
`SojaCoinServices`, `SojaCoinLocks`, and `SojaCoinRedemptions`.

The six logic modules are implementation contracts called by `SojaCoin` using
`delegatecall`. Their ABI files describe those implementations, but operational
state belongs to the SojaCoin contract. Calls that read or change token state
must therefore use the SojaCoin address with the relevant SojaCoin ABI. The
current settings provide one RPC endpoint; resolving different RPC endpoint
references for multiple networks remains future work.

### `blockchain.services.transactions`

- `submit_transaction(record, transaction_hash)` records the network hash and
  moves a prepared transaction to `submitted`.
- `confirm_transaction(record, block_number, block_hash)` moves a submitted
  transaction to `confirmed` only after block data has been read from the
  network.
- `fail_transaction(record, error_message)` keeps a rejected operation visible
  as `failed`.
- `mark_transaction_reorged(record, reason)` removes the completed state when
  a previously observed confirmation leaves the canonical chain.

The transaction lifecycle also validates participant attempts. A participant
transaction cannot be submitted, confirmed, failed or marked reorganized
without its matching saved attempt. A reorganization moves the associated
terms to `needs_review`, so the participant is no longer shown as confirmed.

Each transition locks the row in a database transaction. The functions are
the intended write path for transaction status changes; views and future
blockchain adapters should not edit lifecycle fields directly.

### `blockchain.services.events`

`process_event_once(...)` uses the unique tuple
`network + transaction_hash + log_index` as the idempotency key. It returns
`(event, created)`:

- `created=True`: the event marker was inserted and the optional handler was
  executed in the same database transaction;
- `created=False`: the event was already processed and the handler is not
  called again;
- if the handler raises, the marker is rolled back so a later retry is safe.

### `blockchain.services.transaction_confirmation`

`confirm_transaction_from_chain(transaction_id, event_handler=None)` checks one
submitted transaction against the RPC configured for its saved network. It
returns `None` while the transaction has no receipt, records a reverted receipt
as failed, and records a successful receipt as confirmed. On success, it decodes
events from the transaction's contract ABI when an `event_handler` is provided,
and records each handled log once through `process_event_once`.

Pass `required_event_names=("UnitRegistered",)` for unit registration. Producer
proposals require `ParticipantRegistrationProposed`; terms acceptance requires
both `ParticipantRegistered` and `DepositAuthorizationChanged`. If a successful
receipt lacks a required event, confirmation is rolled back and the transaction
remains submitted.

Pass an operation-specific `event_handler` when a decoded event must also update
business data, such as filling a unit's `chain_unit_id`. The handler runs in the
same database transaction as the event marker and transaction confirmation. If
no handler is passed, the transaction can still be confirmed, but the decoded
events are not marked processed or applied to business records.
This service accepts a transaction database ID, so a temporary management
command can call it now and a future Celery task can call the same service. The
caller is responsible for selecting only submitted transactions.

## Relationship with the companies app

`companies.AccessChange` is the append-only audit record for SaaS membership,
role and unit-access changes. Use the services in
`companies.services.access_history` to deactivate an access record and append
the actor, company, previous value, new value and timestamp. Do not delete an
access record to revoke it.

## How to run the local deployment flow

1. Start Anvil on chain ID `31337`. In Django Admin, create or edit an active
   `Network` record with chain ID `31337` and RPC URL
   `http://127.0.0.1:8545`.
2. Start Django and open **Django Admin → Blockchain → Processos de implantação**.
3. Click **Iniciar implantação**, then select the active company, local network
   and the public wallet address that will own SojaCoin and Unit Registry.
4. On the process detail page, use **Conectar carteira e implantar** for each
   available contract. Confirm each transaction in MetaMask, then use
   **Consultar confirmação** after sending. The next dependent contract unlocks
   only when the receipt has been checked and its address saved.
5. After all nine contracts are confirmed, use **Configurar vínculo** for Unit
   Registry and Issuance Batches. The current SojaCoin owner signs each call
   separately. Use **Consultar confirmação** until each link is confirmed.

The expected end-to-end count is eleven wallet approvals: nine deployments and
two configuration calls. A wallet signature or transaction hash alone does
not mean a contract or link is complete.

Runtime RPC connections use the URL saved on the selected `Network`; they no
longer read a project-wide RPC URL from `.env`. Migration `0009` can use the
legacy `BLOCKCHAIN_RPC_URL` setting once to copy the former global endpoint into
existing network records during upgrade.

## Still outside this step

Automated polling, Celery scheduling, and automatic detection of chain
reorganizations remain future work. Existing transaction services can be called
by a future worker, and the lifecycle service already moves an explicitly
reorganized deployment or configuration process to `NEEDS_REVIEW`.

Unit-registration transactions have a manual reconciliation command; their
automatic worker and MetaMask interaction still need separate follow-up.

### Manual Anvil verification

On 25 November 2026, the operator completed the local flow with Anvil and
MetaMask: all nine contract deployments and both SojaCoin links were confirmed.
The Django database recorded nine confirmed deployment steps and two confirmed
configuration transactions. The automated Django suite still uses simulated
RPC; the isolated test environment cannot start its own Anvil node.

During that run, the third deployment was submitted with nonce `3` while Anvil
expected nonce `2`, so it remained queued. A zero-value transaction at nonce `2`
filled the gap and allowed the already-signed deployment to proceed. When
starting a fresh Anvil chain, clear MetaMask activity and nonce data for that
local network first, or persist the Anvil state between sessions.
