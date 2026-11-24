# Accounts app: implementation plan

This document is the implementation guide for `accounts`. Follow its sequence,
check the current code before each step, and update the status when work is
verified. The user-facing application uses Django templates and Tailwind CSS;
interface text is in Brazilian Portuguese. Code, comments and this README are
in English. Do not introduce a separate frontend or API as a prerequisite.

## Scope and current state

`accounts` owns authentication, user profile and verified links between users
and public wallet addresses. `companies` owns company memberships, roles and
unit access. `participants` will own producer/creditor records and their links
to users. `blockchain` will own networks, contract instances and transactions.
Coordinate across these apps without duplicating their responsibilities.

Current implementation status:

- Django's built-in `User`, sessions and admin are installed; a development
  superuser has been created. Use `settings.AUTH_USER_MODEL` in relationships.
- `UserProfile` is implemented with an idempotent automatic creation signal,
  an existing-user backfill migration, admin registration and model tests.
- `Wallet` is implemented with canonical address normalization, database
  uniqueness, admin registration and model tests. It stores public addresses
  only and does not verify ownership or Safe code.
- `UserWallet` is implemented with one link per user-wallet pair, shared-wallet
  support, verification states, revocation history, admin registration and
  model tests. It does not prove control of the address by itself.
- `WalletVerificationChallenge` is implemented with a unique nonce, mandatory
  network binding, payload history, lifecycle timestamps, admin registration
  and model tests. The account-side wallet rules now enforce ownership,
  verification, expiry and one-time consumption; cryptographic signature
  validation remains an injected blockchain service step.
- Login, POST logout and the authenticated profile are implemented using Django
  templates. Company authorization rules are implemented in `companies`, while
  account-side company selection and post-login context integration remain
  pending. Wallet verification remains pending.
- `companies.CompanyMembership`, `MembershipRole` and `UnitAccess` exist, but
  the complete application authorization and onboarding flows do not.
- The shared `templates/base.html` and Tailwind build exist. The internal layout
  includes a sidebar; authentication screens should not show an empty internal
  navigation. Create a suitable authentication layout when implementing them.
- The email backend currently prints mail to the console. This is for local
  development; it does not deliver invitations or reset messages to users.
- Wallet integration, ownership verification, MetaMask and Safe workflows are
  not implemented in this app.

## Account rules — implemented

The account rules are intentionally small and are implemented in
`accounts/rules/account_access.py`:

1. A login is accepted only when `User.is_active` and `UserProfile.active` are
   both true. The same check runs on every private account view. If an already
   logged-in account is deactivated, the session is ended before the view runs.
2. A missing profile is treated as an inactive account. The profile signal
   normally creates it automatically, but the access rule does not fail open.
3. There is no public registration route. Initial accounts are created by the
   platform team through the assisted Django Admin process.
4. `UserProfileForm` exposes only `full_name` and `phone`. It does not expose
   account activation, Django staff flags, or company roles. Roles belong to
   `companies.MembershipRole` and are changed by the company access flow.
5. Logout is a POST-only class-based view. Django CSRF middleware validates the
   token, and the profile template submits the token in its logout form.

The automated tests in `accounts/tests.py` cover these rules, including an
inactive user, an inactive or missing profile, ignored status fields, the
absence of a signup route, and a logout request without a valid CSRF token.

## Wallet rules — implemented

The wallet rules are in `accounts/rules/wallet_access.py`. They are kept
separate from views so every future class-based view and service can apply the
same checks:

1. `Wallet` stores only a public EVM address. Its `save()` path normalizes and
   validates the address, and the database keeps the normalized value unique.
   No private key or seed phrase field exists.
2. `get_user_wallet()` and `get_user_challenge()` scope lookups to the active
   authenticated user. A different user's wallet or challenge is returned as
   `404`, so an identifier cannot be used to discover another user's link.
3. `require_verified_wallet()` and
   `require_verified_wallet_for_operation()` reject inactive accounts,
   inactive wallets and links that are not in the `verified` state.
4. `require_consumable_challenge()` rejects expired, canceled, consumed,
   revoked-link and inactive-wallet challenges.
5. `consume_wallet_challenge()` locks the challenge row in a database
   transaction, asks an injected signature validator to validate the exact
   challenge, records the signature, consumes the challenge and marks the
   user-wallet link verified. A second request cannot consume the same
   challenge.
6. The signature validator is intentionally injected. The account rule does
   not accept a string as proof of ownership; the future blockchain service
   must validate the EOA signature or the contract wallet's ERC-1271 response
   for the configured network.
7. A verified SaaS link is not an on-chain permission. Before an operational
   transaction, the future blockchain integration must separately confirm the
   wallet's permission in the target contract and unit. These account rules do
   not mark a pending SaaS permission as effective.

The tests in `accounts/tests.py` cover user isolation, inactive accounts and
wallets, pending and verified links, expired challenges, invalid signatures,
successful consumption and replay rejection.

## Shared operation rules — implemented

The cross-app invariants are in `accounts/rules/operation_rules.py`. The
concrete records remain owned by their future apps; these functions are small
validators that those models and services can call without duplicating the
rules:

- `require_registered_participants` requires both producer and creditor before
  a CPR can be prepared.
- `require_accepted_terms` blocks any operation whose terms have not been
  accepted.
- `require_cpr_balance_unchanged` prevents a form or local update from
  changing the outstanding CPR balance. Balance changes must come from a
  confirmed contract event.
- `calculate_net_weight_kg` calculates net weight from gross weight, tare and
  deductions. The submitted net value is not trusted as an independent source.
- `require_unique_weighing_ticket` normalizes the romaneio hash and delegates
  the existing-record lookup to the receipt service, rejecting reuse.
- `require_compatible_batch` requires eligible, unique receipts from one
  producer and applies the contract's maximum of 64 receipts per batch.
- `require_approved_batch_for_issuance` keeps producer approval and token
  issuance as separate stages.
- `can_display_issued_tokens` and `require_confirmed_issuance` expose tokens
  as issued only after the blockchain transaction is confirmed.

These rules do not create participants, CPRs, receipts or transactions. They
will be called by the future `participants`, `cprs`, `receipts`, `issuance` and
`blockchain` apps when those models are implemented.

## Contract facts that affect this app

These facts were checked against the current Solidity sources; recheck the
deployed contract version when implementing integration.

1. `AdministrationModule.proposeParticipantRegistration` is restricted to the
   SojaCoin owner. `acceptParticipationTerms` validates the participant's EIP-712
   signature, deadline and nonce; another wallet may submit a valid signature.
   Creating a Django user or connecting a wallet does not register a participant
   or accept its terms on-chain.
2. `GuaranteesModule.registerCPR` checks the producer's signature. The issuance
   batch contract checks the producer's signature for `approveIssuanceBatch`.
   Approval and `issueTokens` are separate calls. An authenticated Django user
   is not automatically an authorized signer or sender.
3. `UnitRegistry.registerUnit`, `setPermissions` and `setUnitAuditor` require
   its owner. The SojaCoin owner and UnitRegistry owner must be checked
   separately; an application administrator does not become either owner just
   by receiving a SaaS role.
4. The terms, CPR and batch checks use OpenZeppelin `SignatureChecker`, which
   supports signatures from ordinary accounts and compatible contract wallets
   such as ERC-1271 wallets. This does not mean that the SaaS already supports
   Safe signing or its approval workflow. Validate the actual flow and network
   before displaying a Safe operation as supported.

Reference: `contracts/src/contracts/modules/AdministrationModule.sol`,
`GuaranteesModule.sol`, `TokenIssuanceBatches.sol`, and `UnitRegistry.sol`.

## Assisted onboarding for the MVP

The platform team creates the first company and user accounts in an assisted
process, using Django Admin for the MVP. The team explicitly associates each
user with the appropriate company membership or participant record. Do not
expose open self-registration, automatically create a company, or grant an
administrator role merely because of an email address. An invitation and
self-service registration flow is outside the first MVP flow. The existing
development superuser is not a default company administrator or on-chain owner.

## Models

The Django `User` is already present; do not replace it casually after existing
migrations and data. Revisit a custom user model before production only with an
explicit migration plan. The models are implemented in the sequence below. All
new persisted models have `created_at` and `updated_at` using UTC, English field
comments, and migrations. Use database constraints as well as form/service
validation where applicable. Do not treat `Model.clean()` as automatic on every
`save()`.

### 1. `UserProfile` — implemented

- `user`: one-to-one with `settings.AUTH_USER_MODEL`; one profile per user.
- `full_name`, `phone`: display name and optional contact telephone.
- `active`: profile status; access also requires an active Django user.
- `created_at`, `updated_at`.

Profiles are created automatically for new Django users with an idempotent
`get_or_create` signal. The initial migration backfills existing users,
including the development superuser. This profile lifecycle does not assign
business roles; roles remain in `companies`.

### 2. `Wallet` — implemented

- `address`: displayable public EVM address; never store private keys or seed
  phrases.
- `address_normalized`: lowercase canonical `0x` address, validated and unique
  at database level. Keep it consistent with `address` through all write paths.
- `label`: optional user-friendly label; avoid exposing one user's private
  label to another user without an explicit visibility rule.
- `wallet_type`: individual or contract, including Safe; this is a declared
  classification, not proof of control or of the on-chain code at an address.
- `active`, `created_at`, `updated_at`.

An address may be used on multiple EVM networks; ownership checks, contract
wallet verification and transactions must use the relevant chain. A wallet
record alone grants neither application access nor on-chain permission. The
model stores no network-specific ownership proof yet.

### 3. `UserWallet` — implemented

- `user`, `wallet`: foreign keys, unique together; allow a shared corporate
  wallet to be associated with multiple users after each link is verified.
- `verification_status`: pending, verified or revoked.
- `verified_at`, `revoked_at`, `revocation_reason`: verification and revocation
  history.
- `created_at`, `updated_at`.

Verification applies to a particular user-wallet link, not everyone using the
same address. Revocation disables that association without deleting historical
records. Changes of users, wallet signers or relevant on-chain conditions require
the application to reassess whether previous verification is still appropriate.

### 4. `WalletVerificationChallenge` — implemented

The model stores a reference to the intended `UserWallet`, the exact versioned
message and digest, a cryptographically random nonce, intended purpose,
application domain, mandatory chain ID, expiry, status, signature when supplied,
consumption or cancellation timestamps, and `created_at`/`updated_at`. Lookup
fields are indexed and each nonce is unique. The model does not itself verify a
signature or mark a link as verified.

The verification service must bind the challenge to the authenticated user and
intended address; validate signature, domain, purpose, expiry and network; and
consume it atomically once. For an individual address, verify the recovered
signer. For a contract wallet, use the appropriate ERC-1271 validation on the
specified network; a signature from one Safe signer alone does not prove control
of the Safe. Do not claim Safe verification is available until its collection
and validation flow is implemented and tested. A challenge signature is not a
signature accepting terms, a CPR or an issuance batch.

### Account creation and invitation records

The MVP uses assisted account creation in Django Admin; it does not require an
invitation model. If invitations are added later, design their scope, issuer,
expiry, one-use token, status and audit trail before implementation. Coordinate
membership records with `companies` and participant links with `participants`.

## Screens and routes

Login, logout and profile are implemented; the remaining routes are planned.
Keep URL names stable and protect every private view server-side.

| Screen/action | Suggested route | Purpose and states |
| --- | --- | --- |
| Create or activate account | Assisted in Django Admin | The platform team creates initial accounts before first login and explicitly establishes access; no public signup or automatic company privileges. |
| Login | `/accounts/login/` | Username and password for the existing Django user model; missing data, invalid credentials and inactive account. Preserve only safe local `next` redirects. |
| Logout | `/accounts/logout/` | POST with CSRF protection; end the Django session and redirect to a defined page. Disconnecting MetaMask is a separate browser action. |
| Password reset request | `/accounts/password-reset/` | Generic response whether an account exists or not; local email is visible in the console until real delivery is configured. |
| Password reset confirmation | `/accounts/reset/<uidb64>/<token>/` | Validate expiring token and new password; show expired/invalid token and completion states. Reuse Django's authentication facilities. |
| Profile | `/accounts/profile/` | Authenticated user views and edits permitted profile fields. Do not let a profile edit grant roles or change another user. |
| Change password | `/accounts/password/change/` | Authenticated user proves the current password and sets a new one; preserve or invalidate sessions according to the chosen Django flow. |
| Wallets | `/accounts/wallets/` | List this user's links and their pending, verified or revoked state. Provide connect, verification and revoke actions only when implemented. |
| Verify wallet | `/accounts/wallets/verify/` | Show the intended public address, network and message; handle rejected signature, expiry, wrong wallet and wrong network. Never ask for private keys. |
| No company access | Destination to be defined | After login, explain when the user has no active membership. Do not automatically create or grant a company. Producer/creditor access must be handled by their own participant links. |

For login and reset flows, use accessible labels, clear Portuguese messages,
keyboard focus, responsive layouts and no decorative emojis. Link to completed
destinations only; do not expose incomplete account creation or wallet buttons.
An authenticated user can belong to several companies; selection and permission
checks belong to the company flow and must not trust a submitted company ID.

## Business rules and implementation boundaries

- Login establishes a Django session. It does not connect a wallet, register a
  participant, assign a company role or authorize a contract transaction.
- Use Django password hashing and validators, CSRF and session mechanisms.
  Do not store plaintext passwords or wallet secrets. Logout is a POST operation.
- Check `User.is_active`, profile status and the relevant company membership,
  role and unit grant on every protected action. Check participant links for
  producer/creditor data instead of granting staff access. Django superuser
  status is not an automatic business membership or on-chain owner.
- Enforce isolation in queries and forms, not just navigation. Do not reveal
  another company's profile, wallet associations or operational records based
  on a guessed identifier. Decide permitted visibility of shared wallet records
  as part of the company and participant authorization design.
- Verify `next` redirects against allowed hosts. Apply Django's password reset
  token handling and generic responses; configure HTTPS, secure cookies, a real
  email backend and a rate-limiting policy before public deployment.
- Confirm the selected network, contract and on-chain permission for each
  operation. A successful SaaS action or wallet connection is not blockchain
  confirmation. Keep pending, failed and confirmed states separate.
- Preserve access and verification history on deactivation; coordinate SaaS
  revocation with any separate on-chain revocation transaction.

## Ordered implementation and acceptance checks

1. **Prepare assisted onboarding.** The platform team creates initial users and
   companies in Django Admin and explicitly associates users with their company
   memberships or participant records. Do not expose public signup.
2. **Create profile model — complete.** `UserProfile`, its migration, safe
   backfill, automatic creation lifecycle, admin registration and tests are in
   place. The model does not grant roles or blockchain permissions.
3. **Create account access — complete.** `accounts/rules.py` checks the Django
   user and profile before authentication and private views. Logout requires
   POST and CSRF; profile editing is limited to the current user's name and
   phone, without role or activation fields. Django validates the login
   redirect. Routes, templates and tests are in place. Accounts remain created
   through the assisted process in step 1.
4. **Password management — implemented.** Django class-based views handle
   password changes and reset tokens; the response does not reveal whether an
   email exists. The local email backend prints reset links to the console.
   Configure an actual email provider before relying on delivery.
5. **Connect company context — rules implemented; integration pending.** The
   server-side membership, role and unit checks are implemented and tested in
   `companies/rules`. The company list and membership-scoped detail route are
   available. Still integrate the authenticated account flow with explicit
   company selection and a safe post-login destination without assuming that
   every user has a company. Do not create company routes here by implication;
   follow the company plan.
6. **Create wallet models — complete.** `Wallet`, `UserWallet` and
   `WalletVerificationChallenge` are complete with normalization, uniqueness,
   migrations, controlled admin access and tests. Signature verification and
   wallet screens remain separate steps.
7. **Verify individual wallets — account rules implemented.** The account
   rules now scope wallet links to the user, require an active verified link,
   and consume a challenge atomically once it has been validated by an
   injected signature checker. The remaining work is the real EOA signature
   validator, MetaMask screen, challenge creation flow and blockchain-side
   permission check. Add chain-aware ERC-1271/Safe support only when its
   complete signing flow can be tested; show unsupported status until then.
8. **Integrate participant and transaction flows.** The shared operation rules
   are implemented. With `participants` and
   `blockchain`, bind the correct user, wallet, company, network and contract
   to each operation. Test terms, CPR and batch signatures against the deployed
   contract and retain separate approval/issuance confirmations.

Run Django checks and targeted automated tests for each completed step. Record
what was implemented and what remains; do not mark a UI, verification or
blockchain operation complete merely because a model or screen exists.

See [the MVP plan](../planejamento.md), [the companies plan](../companies/README.md)
and [the screen design brief](../screams.md) for the rest of the system.
