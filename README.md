# SojaCoin

SojaCoin connects soybean warehouse operations to an EVM blockchain. The MVP records a participant, a soybean delivery, its weighing data and documents, then lets the participant approve an issuance batch and receive SJC tokens in a wallet. The application is designed for the Brazilian agricultural market and has been demonstrated on BNB Smart Chain Testnet.

**One SJC represents 60 kg of recorded net soybean weight.** The token has six decimal places. Blockchain records make submitted information traceable; they do not independently verify the physical inventory or establish legal rights over it.

## What the MVP does

1. A company registers a warehouse unit and authorizes separate wallets for receiving loads and creating issuance batches.
2. The company publishes a terms PDF and proposes a participant's registration. The participant reviews the terms and accepts them with their wallet.
3. A receiving employee records a load, weighing information, deductions, a weighing ticket PDF and an electronic invoice (NF-e). The application calculates the net weight and registers the receipt on-chain in three steps.
4. An issuance employee creates a batch for one confirmed receipt. The participant reviews it and confirms one transaction that approves the batch and issues SJC.
5. The application shows the confirmed issuance and the participant's current balance. The token can also be imported into a compatible wallet.

The PDF files are stored privately by the application. The contract receives the weighing ticket PDF's hash and the hash of the NF-e access key; the invoice PDF is not sent to the blockchain. Audit preparation is currently manual. CPR and audit management screens are not part of the demonstrated SaaS flow.

## Requirements

- Python **3.14** and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Node.js and npm for the frontend build.
- [Foundry](https://getfoundry.sh/introduction/installation/) for Anvil and contract tests.
- A browser with MetaMask or another compatible browser wallet. Use separate test accounts for the contract owner, receiver, issuer, auditor and participant.
- Git to clone the repository.

## Run the application

From a terminal, clone the repository and install the application dependencies:

```bash
git clone https://github.com/ygorlima-py/sojacoin-SmartContract.git
cd sojacoin-SmartContract/app
uv sync
npm ci
npm run css:build
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver 127.0.0.1:8001
```

Keep this terminal open. Visit **http://127.0.0.1:8001/admin/** and sign in with the superuser just created. The regular application login is at **http://127.0.0.1:8001/accounts/login/**. To use port 8000 instead, run `uv run python manage.py runserver` and replace `8001` in the URLs.

The database is `app/db.sqlite3`; private uploads are stored in `app/private_media/`. These files are local and are not included in the repository. Run `migrate` again after pulling changes that add migrations. The default Django configuration is for local development, not production.

## Prepare a local blockchain

In a second terminal, start Anvil and keep it running:

```bash
anvil --chain-id 31337 --state /tmp/sojacoin-anvil.json
```

Anvil listens at `http://127.0.0.1:8545` and prints funded test accounts and their private keys. These keys are public test credentials: use them only on the local chain. The state file preserves the chain when Anvil exits normally; keep the Django database and private uploads as well if you want to resume the same demonstration.

Add a custom network to MetaMask with RPC URL `http://127.0.0.1:8545`, chain ID `31337`, and currency symbol `ETH`. Import the Anvil test accounts you need and select the owner account for deployment. MetaMask and Django must reach the same Anvil instance. If Anvil is reset but MetaMask still has activity from the old chain, clear the local network's activity/nonce data in MetaMask.

Alternatively, configure BNB Smart Chain Testnet (chain ID `97`) in MetaMask and Django, using an accessible testnet RPC. Each wallet that sends a transaction needs testnet BNB for fees.

## First-time company and contract setup

1. In Django Admin, create an active **Company** and an ordinary active user for the company administrator under **Users**. Use a valid test CNPJ for the company.
2. Under **Company memberships**, create an active membership linking that user to the company. Under **Membership roles**, grant that membership the active **Administrator** role. Select the Django superuser as **Granted by**: the recipient cannot grant a role to themselves. A Django superuser alone does not automatically have a company membership or company role.
3. Under **Blockchain → Networks**, create an active network with the same chain ID and RPC as the wallet: `31337` and `http://127.0.0.1:8545` for local Anvil, or `97` and the selected RPC for BNB Testnet.
4. Under **Blockchain → Deployment processes**, start a deployment for the company and network, entering the public address that will own SojaCoin and UnitRegistry. Follow the [deployment steps below](#deploy-and-link-the-contracts).
5. Sign in to the SaaS as the company administrator, select the company under **Companies**, and create a unit under **Units**. Register it on the blockchain from its detail page with the UnitRegistry owner wallet, then confirm the transaction.
6. Create separate staff users in Django Admin. In the SaaS, add them under **Team and permissions**, assign **Receiving** or **Issuance**, and grant the appropriate access to the unit. Each employee signs in to their own account and verifies their wallet under **My profile → My wallet**. Back on the unit page, the administrator grants each verified wallet its corresponding on-chain permission using the UnitRegistry owner wallet.
7. Prepare an audit using the [manual steps below](#prepare-the-audit-in-remix). Every active unit must have a current audit for the issuance workflow.

The full deployment procedure is documented in the [blockchain app README](app/blockchain/README.md#how-to-run-the-local-deployment-flow).

### Deploy and link the contracts

1. In MetaMask, select the owner wallet and the same network configured in Django Admin. Make sure the wallet has native test currency for deployment fees.
2. Open **Blockchain → Deployment processes → Start deployment** in Django Admin. Select the company and network, enter the owner's public address and start the process.
3. On the process page, click **Connect wallet and deploy** for the available contract. Confirm the request in MetaMask, then click **Check confirmation**. Wait until the contract is confirmed before proceeding to the next available step.
4. Repeat until all **nine contracts** are confirmed. Their addresses appear in the process and in the contract records in Django Admin. The application prepares each contract and its constructor arguments; no manual constructor input is needed in MetaMask.
5. In **Contract links setup**, click **Configure link** for UnitRegistry and confirm the transaction. Check its confirmation, then repeat for TokenIssuanceBatches. Both links must be confirmed before the deployment is ready.

This requires **11 transactions** in total: nine deployments and two configuration calls. Copy the SojaCoin and UnitRegistry addresses from this deployment; the SojaCoin address is also the SJC token address. If the wallet returns a transaction hash but the application cannot confirm it yet, use the existing confirmation or hash-recovery control rather than submitting a second transaction.

### Prepare the audit in Remix

The audit is currently a manual contract operation. Complete the unit registration and note its **ID na rede** and the deployed **UnitRegistry address** first. The auditor must use a separate wallet that does not have conflicting receiving or issuance permissions. The wallet submitting each transaction needs native test currency.

1. Open [Remix IDE](https://remix.ethereum.org/) in the browser with MetaMask installed. Create a Solidity file, for example `AuditInterface.sol`, and paste this interface:

   ```solidity
   // SPDX-License-Identifier: MIT
   pragma solidity ^0.8.20;

   interface IUnitRegistryAudit {
       function setUnitAuditor(uint256 unitId, address newAuditor) external;
       function recordUnitAudit(
           uint256 unitId,
           uint256 physicalInventoryKg,
           bytes32 reportHash
       ) external;
       function auditorByUnit(uint256 unitId) external view returns (address);
       function allUnitAuditsCurrent() external view returns (bool);
   }
   ```

2. Compile the file. In **Deploy & Run Transactions**, choose **Browser Extension** with MetaMask as provider; some Remix versions call it **Injected Provider**. Connect MetaMask to the same network as the deployment.
3. Select `IUnitRegistryAudit` and use **At Address** or **Add Contract** to attach it to the **existing UnitRegistry address**. Do not deploy the interface.
4. Select the UnitRegistry **owner** wallet in MetaMask. In `setUnitAuditor`, enter the unit's on-chain ID and the auditor's public address, then confirm the transaction. Read `auditorByUnit` with the same ID to check the assignment.
5. Switch MetaMask to the assigned **auditor** wallet. In `recordUnitAudit`, enter the unit ID, the physical inventory in whole kilograms, and a 32-byte report hash. For a new, empty test unit, the inventory can be `0`; this sample hash is only for demonstration:

   ```text
   0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
   ```

   Confirm the transaction and wait for success. In a real operation, the hash must correspond to the actual audit report.
6. Call `allUnitAuditsCurrent()`. It must return `true`; repeat the audit setup for each active unit if necessary. The contract's audit period is 60 days.

For a new load added after an empty-unit audit, use a weighing timestamp after that audit and no later than the current blockchain time. The subsequent receipt updates the recorded inventory.

## Walk through the MVP

1. As the company administrator, open **Company → Manage terms** and publish a test terms PDF. Under **Participants**, register a participant's name and public wallet, then propose the registration using the SojaCoin owner wallet.
2. Once the proposal is confirmed, create and copy the acceptance link. The participant opens it, reviews the PDF, connects their own wallet, signs the terms and confirms a separate transaction. Check the registration confirmation.
3. Sign in as the receiving employee. Under **Receipts**, create a receipt with the confirmed participant, authorized unit, crop season, weights, deductions, NF-e key and PDFs. Open its detail page and confirm the three blockchain calls: open the receipt, confirm weighing, and link the invoice.
4. Sign in as the issuance employee. Open **Issuances**, select the confirmed receipt and create an issuance batch with an approval deadline. Once confirmed, create and copy its participant approval link.
5. The participant opens the link, reviews the load and confirms **Approve and receive tokens** with the registered wallet. That transaction approves the single-receipt batch and issues the tokens. Check the confirmation and the amount shown on the receipt page.
6. In MetaMask, select the participant account and the same network. If SJC is not listed, import the **SojaCoin contract address** from the deployment; the token uses six decimals. The participant also needs native test currency for transactions they send.

Use a distinct weighing ticket PDF for each load: the contract rejects a weighing ticket hash that has already been used. A transaction can remain pending until its receipt is available; use the page's confirmation controls or recover the existing transaction hash before attempting another send.

The local URLs above work on the computer running Django. On another device, `127.0.0.1` points to that device itself. Remote testing requires an appropriately configured reachable Django URL and RPC; see the [participant link documentation](app/participants/README.md#public-link-and-phone-testing).

Contract changes require compiling and updating the application's packaged ABI and bytecode before a new SaaS deployment. Previously deployed contracts and their on-chain state do not update automatically.

## Project structure and documentation

- [`app/`](app/) — Django application, templates and frontend assets.
- [`app/blockchain/`](app/blockchain/README.md) — network, deployments and transactions.
- [`app/companies/`](app/companies/README.md) — companies, units, staff and access.
- [`app/participants/`](app/participants/README.md) — participant registration and terms.
- [`app/receipts/`](app/receipts/README.md) — load receipts and documents.
- [`app/issuance_batches/`](app/issuance_batches/README.md) — batches and token issuance.
- [`contracts/`](contracts/README.md) — Solidity contracts and Foundry walkthrough.
- [Application plan](app/planejamento.md) and [contract architecture](contracts/docs/arquitetura.md) — Portuguese documentation.

The interface supports Portuguese and English. See the [application README](app/README.md) for frontend builds and translations.

## License

SojaCoin is available under the [MIT License](LICENSE).
