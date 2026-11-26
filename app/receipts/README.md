# Receipts

The `receipts` app stores a company's soybean load receipt in the SaaS. It
records the participant, an authorized storage unit, crop year, integer-kilogram
weighing values, classification percentages, applied deductions, invoice key and
the supporting PDFs.

The app saves the local record and, from its detail page, can run the ordered
on-chain receipt workflow. The local entry itself never sends a transaction.
On-chain completion is shown only after the configured RPC confirms the
transaction, the expected event, and the resulting contract state.

## Who can create and view receipts

The signed-in user must:

1. Have an active membership and the receiving role in the company from the URL.
2. Have active receiving access to the selected unit.
3. Select an active unit that already has a confirmed `chain_unit_id`.
4. Select a participant from that company whose registration status is confirmed.

Every list, detail and PDF request repeats the company and unit checks on the
server. IDs submitted by the browser do not establish access. PDFs are stored
under `DJANGO_PRIVATE_MEDIA_ROOT` and streamed only through the authorized
document route; do not publish that directory through a static or public media
URL.

## Receipt data and contract-compatible encoding

- `Receipt.crop_year` keeps the readable value, for example `2026/2027`.
- `Receipt.crop_year_bytes32` keeps the UTF-8 bytes right-padded to 32 bytes.
- Weights and applied deductions are integer kilograms. The server calculates
  `net_weight_kg = gross_weight_kg - tare_weight_kg - all_deductions_kg` and
  rejects a result of zero or less.
- Classification percentages use `Decimal` input and are saved as hundredths of
  a percent in `moisture_bps`, `impurities_bps` and `damaged_grain_bps`. Thus
  `15.50%` is stored as `1550`, matching the Solidity `uint16` fields.
- The weighing ticket PDF is required; a classification report PDF is optional.
  Their Keccak-256 hashes are calculated over the exact uploaded bytes.
- The NF-e access key is stored separately from its PDF. Its 44 digits are
  Keccak-256 hashed for the contract's `taxInvoiceKeyHash`; the PDF has its own
  independent hash in `ReceiptDocument.file_hash`.
- PDF files are limited by `RECEIPT_DOCUMENT_MAX_PDF_SIZE`, defaulting to 10 MiB.
  The server checks the `.pdf` extension, size and PDF markers, then stores a
  random path instead of the user-provided filename.

The hash identifies the submitted bytes; it does not prove that the document's
contents or the physical load are accurate.

## Operator flow

1. Open **Recebimentos** in the company sidebar.
2. Select **Novo recebimento**.
3. Choose the participant, authorized unit and crop year.
4. Enter gross weight, tare, classification percentages, each applied
   deduction, and the date and time shown on the weighing ticket.
5. Attach the weighing-ticket PDF, NF-e access key and NF-e PDF. Attach a
   classification report if the load has one.
6. Review the displayed net-weight preview and save. The server recalculates
   that value before writing the record.
7. Open the receipt detail to review the saved information and private PDFs.
8. Choose an eligible network and select **Conectar carteira e enviar**. The
   connected wallet must belong to the signed-in user, be verified in **Meu
   perfil**, have receiving access to the unit in the SaaS, and have a
   confirmed receive permission in that unit's active `UnitRegistry`.
9. Confirm the first MetaMask request to open the receipt. After the server
   confirms `LoadReceiptOpened` and verifies the on-chain deposit, the page
   displays its `depositId`.
10. Confirm a second MetaMask request to register the weighing fields and the
    Keccak hash of the exact stored weighing-ticket PDF. The server verifies
    both `LoadReceiptConfirmed` and `ReceiptClassified`, then checks the
    deposit's saved weight, weighing hash and timestamp at the event block.
11. Confirm a third MetaMask request to link the NF-e. The contract call uses
    the Keccak hash of the 44-digit access key. The NF-e PDF remains private in
    SaaS storage; its separate file hash is not the argument to
    `linkTaxInvoice`.
12. Use **Consultar confirmação** on each submitted step. The receipt is
    marked confirmed and ready for a later batch only after the invoice event
    and final on-chain deposit state match the local records.

The browser preview is informational. The server is authoritative for the
stored weight and all validation.

## Issuance batch handoff

After the three receipt calls are confirmed, a user with the company's issuance
role and issuance access to that unit can open the receipt detail and create a
batch. The user does not need the receiving role to view the receipt or start a
batch. The next step uses the separately configured `TokenIssuanceBatches`
contract and checks the load, unit, audit, issuance permission and deployment
links against the same saved network as the receipt. See
[`issuance_batches/README.md`](../issuance_batches/README.md) for the batch and
participant approval flow.

## On-chain call order and recovery

The calls run in this order, matching the current SaaS workflow:

1. `openLoadReceipt(participant, unitId, cropYear)` opens the load and records
   the deposit ID returned by the `LoadReceiptOpened` event.
2. `confirmLoadReceipt(depositId, weighingTicket)` sends the Solidity struct
   with its exact field order, integer kilograms, classification basis points,
   the PDF hash, optional report hash and weighing timestamp.
3. `linkTaxInvoice(depositId, taxInvoiceKeyHash)` links the NF-e access-key
   hash. The contract accepts it after the weighing confirmation.

Each call creates its own immutable attempt and `BlockchainTransaction` before
the wallet prompt. The server fixes the network and both contract instances on
the first attempt. Each later call is rechecked against the same network and
deployments. A different authorized receiver may continue a later step; the
wallet that opened the deposit is kept separately as evidence.

If MetaMask explicitly rejects a request, that attempt is marked failed and a
new attempt can be prepared. If the wallet response or hash submission is
uncertain, the page asks the operator to recover the same attempt with its
transaction hash; it never sends the call again automatically. A submitted
hash is checked against the RPC sender, chain ID, SojaCoin address, zero native
value and exact prepared calldata. The confirmation action also requires the
expected event and reads the deposit at the event block. Reorganizations mark
the operation for review.

Every write requires a signed-in receiver role, active unit access and a
verified wallet link. The RPC independently verifies the unit registration,
active participant terms and live receive permission for the connected wallet.
No private key is stored or sent to Django. Each of the three calls requires a
separate wallet transaction confirmation. The invoice PDF and weighing PDFs
remain private SaaS documents; only the specified hashes and structured values
are sent to the contract.
