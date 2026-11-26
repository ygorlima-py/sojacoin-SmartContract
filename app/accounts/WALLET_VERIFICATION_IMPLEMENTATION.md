# Wallet linking and verification implementation checklist

## Goal

Let an authenticated employee link a public EVM wallet to their own SaaS account
from **Meu perfil**, prove ownership by signing a short-lived challenge in
MetaMask, and then appear as a selectable person in the existing unit wallet
permission screen. Never accept or store a private key or recovery phrase.

## Existing rules to preserve

- Reuse `Wallet`, `UserWallet`, and `WalletVerificationChallenge`.
- Reuse `consume_wallet_challenge()` and account ownership/expiry rules.
- Scope every wallet and challenge lookup to `request.user`.
- Use a configured active `Network` chain ID for every challenge.
- Support MetaMask EOA signatures for this flow. Do not claim Safe/ERC-1271
  signature support unless it is implemented and tested.
- A verified SaaS wallet link is separate from an on-chain unit permission.

## Ordered work

1. **Inspect integration points** — profile view/template, account URL patterns,
   wallet models/rules/admin, installed crypto dependencies, and current tests.
2. **Add wallet verification service** — create or reuse a normalized wallet
   link, issue an expiring one-time message challenge for an active network,
   verify an EIP-191 `personal_sign` signature against that exact message and
   linked address, then call the existing atomic consume rule. Preserve prior
   challenges; never reopen a consumed or cancelled challenge.
3. **Add authenticated endpoints** — POST-only, CSRF-protected endpoints to
   start a challenge and submit its signature. Validate JSON/input fields,
   account ownership, active wallet, and chain ID. Return only public address,
   challenge message/ID, and expiry needed by the browser.
4. **Build profile experience** — add the “Minha carteira” section with
   connect, sign, pending, verified, and error states. Load an accessible
   JavaScript module that uses `window.ethereum`, asks for the account, checks
   its chain against the selected active network, requests `personal_sign`,
   and posts the signature with CSRF protection.
5. **Connect unit permission selection** — ensure the existing candidate query
   picks up a newly verified `UserWallet`; expose current wallet state on the
   profile without exposing other users' private labels or challenge data.
6. **Test** — valid and invalid signatures; expired and replayed challenge;
   another user's challenge; already-linked wallet; duplicate address; missing
   or inactive network; CSRF/method/auth checks; profile rendering; candidate
   availability only after verified link plus role and unit access.
7. **Document and verify** — update `accounts/README.md`, run account tests,
   migration checks and Django system checks, and mark this checklist complete.

## Progress

- [x] Step 1: inspected existing models, rules, profile, URLs, and dependencies.
- [x] Step 2: verification service and cryptographic validation.
- [x] Step 3: authenticated challenge/signature endpoints.
- [x] Step 4: profile UI and MetaMask client.
- [x] Step 5: verified wallets appear in the existing unit permission selector
      when role and unit access are active.
- [x] Step 6: automated tests for signatures, expiry, ownership, duplicate
      links, network checks, CSRF, replay and unit selection.
- [x] Step 7: account documentation, full account test suite, Django checks,
      migration consistency check and whitespace check.

Update this checklist after each step. Re-read it before changing scope or
moving to the next step.
