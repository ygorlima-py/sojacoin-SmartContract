# SojaCoin

SojaCoin is a software project for recording soybean storage and related
obligations. It combines a Django web application with smart contracts for an
Ethereum-compatible blockchain. The intended users include warehouse
companies, soybean producers, creditors, auditors and warehouse employees.

The project is being developed for the Brazilian agricultural market. Its
contracts and application are at different stages of development: the full
business workflow is not yet available through the web application.

## The agricultural context

In Brazil, soybeans are often measured in **sacas**: a saca is a 60 kg bag,
used as a commercial unit of measure. A producer can deliver soybeans to a
warehouse for storage and may have an obligation to deliver part of that crop
to a creditor.

A **CPR** (*Cédula de Produto Rural*, or rural product note) is a Brazilian
instrument used to formalize an obligation related to agricultural products.
In the flow modeled here, a producer can owe a quantity of soybeans to a
creditor. The contracts track the obligation and apply eligible deliveries to
its outstanding balance.

The system uses **SJC**, a token representing stored soybean inventory: one
SJC represents one 60 kg saca. It supports fractions of a saca with six decimal
places. The token is backed by inventory recorded through the system's audit
and receipt rules; the blockchain cannot independently inspect the physical
soybeans.

## How the intended workflow works

1. A warehouse company is set up with its own contracts and storage units.
2. An assigned auditor records an audit and the inventory of each unit.
3. Producers and creditors are registered, and participants accept the terms.
4. A producer's CPR is recorded and signed, identifying the creditor, quantity,
   unit and crop year.
5. A warehouse employee records a delivery, including the net weight and its
   supporting documents. The tax invoice is linked to the delivery.
6. An authorized employee creates an issuance batch. The producer approves it
   with a wallet; issuance is a separate operation performed by an authorized
   wallet.
7. Once issuance is confirmed, SJC is allocated to creditors for compatible
   outstanding CPRs, and any remainder goes to the producer.

For example, if an eligible delivery corresponds to 500 sacas and the producer
owes a creditor 400 sacas under a compatible CPR, issuance allocates 400 SJC to
the creditor and 100 SJC to the producer. Compatibility includes the producer,
storage unit and crop year; the contract applies its CPR ordering rules.

Documents are kept outside the blockchain. Their hashes can be recorded on the
blockchain to identify the corresponding document or record. A hash alone does
not prove that the information in a document is accurate.

## Project structure

| Directory | Purpose |
| --- | --- |
| [`app/`](app/) | Django application, server-rendered templates and Tailwind CSS. |
| [`app/accounts/`](app/accounts/) | User accounts, profiles and wallet-related records. |
| [`app/companies/`](app/companies/) | Companies, storage units, staff roles and unit access. |
| [`app/blockchain/`](app/blockchain/) | Network connections, deployed contract records, transactions and reconciliation. |
| [`contracts/`](contracts/) | Solidity contracts, Foundry configuration and contract tests. |

Each company has its own contract deployment on a selected network. The
deployment consists of six logic modules, SojaCoin, UnitRegistry and
TokenIssuanceBatches. The main SojaCoin contract uses the logic modules; the
other two contracts manage unit-related operations and issuance batches.

## Current implementation

The Django application includes company and unit management, team membership,
roles, unit access and access history. Unit registration is currently local:
saving a unit in the application does not by itself register it on the chain.
Initial company and user setup is assisted through Django Admin.

The blockchain app can connect to the RPC endpoint configured for a network,
track transactions and use the Admin deployment flow. The current flow uses a
browser wallet for nine individual deployments, followed by two configuration
transactions signed by the SojaCoin owner. The application checks the network
and transaction receipts before recording confirmations. It does not store user
private keys.

Remote owner approval, Safe integration, automatic transaction polling and
automatic chain-reorganization detection are future work. The end-to-end SaaS
screens for participants, CPRs, receipts, audits and issuance are also still
being built. Contract capabilities should not be assumed to be available in
the application interface.

## Local development

The application requires Python 3.14 or later, [uv](https://docs.astral.sh/uv/)
and Node.js. From the repository root:

```bash
cd app
uv sync
npm ci
npm run css:build
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

The local Django database is `app/db.sqlite3`. The default settings are for
development and need to be changed before production use. See the
[application README](app/README.md) for frontend development details.

For contract development, install [Foundry](https://getfoundry.sh/) and the
contract dependencies:

```bash
cd contracts
npm ci
forge build
forge test
```

For the current local deployment flow, start Anvil, configure its RPC URL and
chain ID in a Django Admin **Network** record, and connect a test wallet to the
same network. The [blockchain app README](app/blockchain/README.md) describes
the Admin flow. The [contracts README](contracts/README.md) provides a separate
contract-level walkthrough of a delivery and issuance.

## Further documentation

- [Application plan](app/planejamento.md) — intended MVP and remaining work, in Portuguese.
- [Blockchain plan](app/blockchain/PLAN.md) — integration and deployment work.
- [Contract architecture](contracts/docs/arquitetura.md) — contract design, in Portuguese.
- [Contract walkthrough in Portuguese](contracts/README.pt-BR.md).
