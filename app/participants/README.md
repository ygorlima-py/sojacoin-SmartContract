# Participant registration

The `participants` app records a participant for one SaaS company and follows the
participant's registration on that company's deployed SojaCoin. A participant does not
need a Django account or a company-team role to accept the terms. The employee
prepares the proposal; the participant reviews the company's PDF and accepts it
through an individual link in the participant's own browser.

This app covers participant registration only. It does not implement receiving,
CPRs, issuance, or audits.

## Company terms PDF

The company supplies the PDF it has approved for use with its participants. A user
with the active **Administrator** role for that company publishes it from
**Empresa → Termos dos participantes**. In the current demo, Gilberto can do this for
Boasafra because his account has that role. The employee who creates participant
records does not write or upload a new terms document for each person.

The upload page supports drag and drop or file selection. It requires a PDF of
up to 10 MB and an explicit confirmation that the company approved the file.
The SaaS stores the original bytes, records who published the version and when,
and calculates a Keccak-256 hash from those exact bytes. The contract receives
that hash as `bytes32`; the PDF remains in private Django storage and is served
only through company membership or the individual acceptance link.

Publishing a different PDF creates the next company version and makes it current
for new participant records. Previous PDFs and hashes remain unchanged. Existing
participant records continue to reference the version they received. The SaaS
records the company's approval statement; it does not review the document or
certify that it has legal approval.

## What is stored

- The participant's company, display name, normalized public wallet address and
  record creator.
- The immutable PDF version associated with that participant, its Keccak-256 hash,
  proposal state and any later acceptance state. Legacy text-test records remain
  readable and are not rewritten by the migration.
- The selected network and SojaCoin instance, each proposal and acceptance
  attempt, prepared call data and associated `BlockchainTransaction`.
- A random, time-limited acceptance link as a SHA-256 digest. The original
  secret is returned to the employee only when the link is created and is not
  stored in the database.
- The participant's EIP-712 challenge, deadline and signature, so an interrupted
  acceptance can be checked against the exact message that was signed.

No private key is requested or stored.

## Before using the screens

1. Apply migrations from the `app/` directory:

   ```bash
   uv run python manage.py migrate
   ```

2. In Django Admin, configure an active `Network` with its chain ID and RPC URL,
   and an active SojaCoin `ContractInstance` for the company and network.
3. Add an active company membership for the employee and grant the
   **Administrator** or **Registration operator** function.
4. Sign in as the company's **Administrator**, open **Empresa** in the sidebar,
   choose **Gerenciar termos**, upload the approved PDF and confirm publication.
5. In a local development environment, make sure the browser wallet and Django
   can both reach the selected RPC. Use only a test wallet and test network.

The PDF files use Django's default storage under `DJANGO_PRIVATE_MEDIA_ROOT`
(default: `app/private_media/`). Do not serve this directory as a public media
folder. In a deployed environment, point Django's default storage to durable,
private storage and back it up. The default upload limit is 10 MiB; configure
`PARTICIPANT_TERMS_MAX_PDF_SIZE` in bytes if the deployment needs a different
limit. The RPC URL is read from the selected network record. The server checks
its reported chain ID before using it.

## Employee flow

1. A company administrator publishes the company's current terms PDF as
   described above.
2. Open **Participantes** under the company in the SaaS.
3. Create a participant using their name and public wallet address. The form shows
   the exact current version and hash; that version is saved with the participant.
4. Open the participant record, choose the network containing the company's active
   SojaCoin and select **Conectar carteira dona e propor cadastro**.
5. Connect the current SojaCoin owner wallet in that browser. The owner confirms
   one blockchain transaction. The employee does not enter a private key into
   Django.
6. Wait for the transaction to appear on the selected RPC, then use
   **Consultar confirmação**. The app checks the receipt, the
   `ParticipantRegistrationProposed` event and the pending proposal and PDF hash
   in the contract.
7. Once the proposal is confirmed, create an acceptance link. Copy it and give
   it to the participant yourself. The app does not send the link by email or
   message.

## Participant flow

1. The participant opens the individual link in their own browser or phone. The
   page shows the company, participant wallet and the exact PDF version saved with
   that proposal. The PDF can be viewed in the page or opened in another tab.
2. The participant connects the wallet shown in the proposal on the selected
   network.
3. The wallet asks the participant to sign an EIP-712 message. This signature does
   not send a transaction and does not pay gas.
4. The server validates the EIP-712 domain and fields against the contract's
   `participationTermsAcceptanceHash` result, the current proposal, nonce,
   network, contract, wallet and deadline.
5. The participant confirms a second wallet prompt to send
   `acceptParticipationTerms`. The participant's wallet sends the transaction and
   pays the network fee; the SaaS does not sign or send it for them.
6. The participant or employee uses **Consultar confirmação**. The app requires a
   successful receipt, both expected events, and the on-chain
   `participantRegistered` and `depositAuthorizationActive` values before it
   displays **Cadastro confirmado**.

The owner makes one transaction to propose the registration. The participant
signs one message and sends one transaction to accept it.

## Status and recovery

- **Cadastro criado**: the participant record is attached to the company's PDF
  version current at the time of creation.
- **Proposta preparada / enviada**: the owner's call is saved or has a hash;
  this is not yet an invitation to accept.
- **Aguardando aceite do participante**: the proposal receipt, event and contract
  state were checked successfully.
- **Aceite preparado / enviado**: the participant signed and submitted the
  acceptance call, but the final chain checks are still pending.
- **Cadastro confirmado**: receipt, both events and contract storage were
  checked.
- **Falha na transação**: a wallet explicitly rejected the request or the
  network receipt reverted. A new attempt can be prepared where allowed.
- **Precisa de conferência**: required evidence does not match, or a previously
  confirmed transaction was marked reorganized.

If the wallet returns a transaction hash but Django cannot record it, enter
that same hash on the existing attempt. An uncertain wallet response keeps the
attempt open; it does not automatically create or send another transaction. An
unused acceptance link can be canceled. Creating a replacement link cancels the
previous unused link, and the complete URL is shown only once. Links expire
after seven days.

## Public link and phone testing

Set `PARTICIPANT_PUBLIC_BASE_URL` to the externally reachable base URL when the
link must be copied to another device, for example the HTTPS URL of a test
environment. Without it, Django builds the link from the current request host.

An Anvil URL such as `127.0.0.1:8545` points to the device currently using it.
A phone cannot reach an Anvil process running at `127.0.0.1` on the employee's
computer. For a phone demonstration, both devices need a network/RPC endpoint
they can reach and a Django URL reachable by the phone. Do not expose a local
development RPC or test wallet to the public internet.

Only individual EOA wallets compatible with MetaMask are supported for the
participant's EIP-712 signature. Contract wallets such as Safe receive a clear
unsupported-wallet message; EIP-1271 verification is not implemented here.

## Tests

Run the app tests from `app/`:

```bash
uv run python manage.py test participants
```

The tests use a temporary Django test database and mocked RPC responses. For a
manual Anvil run, use separate test accounts for the SojaCoin owner and the
participant, set the network record to the Anvil chain ID and RPC URL, and follow
the employee and participant flows above. A real MetaMask prompt is required to
verify the browser-to-wallet interaction; automated tests do not open a wallet.
