# Companies app

This app manages companies, their units, staff memberships and access within the SojaCoin SaaS. It provides the company and unit context used to isolate each customer's operational data.

## Current status

- The app is registered in Django and included under `/companies/`.
- The company, membership, role, unit, unit-access, wallet-permission and access-history models are implemented, migrated and registered in Django Admin.
- `AccessChange` is append-only. Use `companies.services.access_history` to deactivate memberships, roles or unit grants and record the actor, company, before/after values and timestamp.
- The Django Admin hooks for memberships, roles and unit grants call the same
  history service after assisted changes. Those records cannot be deleted from
  the Admin; target relationships are immutable after creation, so a correction
  is made by revoking the old grant and creating a new one.
- Company selection and membership-scoped detail views, company access rules,
  unit registration hashing, local unit persistence and access-history services
  are implemented.
  The assisted forms, company update flow and unit views are implemented.
  Adding an existing Django user to a company team and managing membership
  roles, SaaS unit-permission management and access-history consultation are
  implemented. Blockchain operation views remain planned.

## Assisted company form — implemented

`companies.forms.CompanyForm` is the first step of the assisted company
onboarding flow. It accepts only `legal_name`, `trade_name` and `cnpj`.

- CNPJ input is normalized before validation and storage.
- CNPJ check digits are validated using the same model rule used by `Company`.
- A normalized CNPJ already used by another company is rejected.
- `active` is not a form field, so a submitted value cannot activate or
  deactivate a company. When editing, the existing status is preserved.
- There is no public company-creation route. The platform-assisted creation
  flow will use this form when its authorized view is implemented.

## Unit form — implemented

`companies.forms.UnitForm` accepts `name`, `code`, `address`, `cnpj` and
`state_registration`. The company is supplied by the server-side company
context; it is not a user-editable form field.

The form does not expose `registration_hash`, `chain_unit_id` or `active`.
Those values remain under system control. Form validation normalizes the unit
code and CNPJ, validates the CNPJ check digits, requires the canonical
registration fields and rejects duplicate CNPJ or company-scoped unit code.
Hash calculation and blockchain registration are handled by the next
onboarding step, not by this form.

## Company Admin unit inline — implemented

When editing a company in Django Admin, the **Units** inline allows the
platform team to add or edit that company's units on the same page. The inline
accepts only `name`, `code`, `address`, `cnpj` and `state_registration`.

The parent company is supplied by Django Admin and is not accepted as a
submitted field. The inline saves through
`companies.services.unit_registration.save_unit_registration`, so it also
normalizes the values and calculates `registration_hash`. New units keep
`chain_unit_id` empty; no blockchain transaction or RPC call is created.

## Local unit registration persistence — implemented

Creation and editing must use the following sequence in the authorized view:

1. Validate `UnitForm`.
2. Pass `form.instance` and `form.cleaned_data` to
   `companies.services.unit_registration.save_unit_registration`.
3. Let the service normalize the canonical fields, calculate the hash and
   save the Django record.

For a new unit, the service always clears `chain_unit_id`. For an existing
unit, an already confirmed `chain_unit_id` is preserved while the local
registration hash is recalculated. This service does not create a
`BlockchainTransaction` and does not call an RPC endpoint. Blockchain
registration is a later, separate operation.

## Confirming an on-chain unit registration — implemented

`companies.services.unit_registration.handle_unit_registered_event` applies a
decoded `UnitRegistered` event to the local unit. Before saving
`chain_unit_id`, it checks that:

- the event belongs to a `unit_registration` operation using a `UnitRegistry`;
- the event's registered wallet matches the transaction's saved sender;
- the unit code and registration hash match a unit owned by the transaction's
  company;
- the local unit has no conflicting on-chain ID.

The handler locks the local unit while checking and saving it. The reconciliation
command requires the `UnitRegistered` event before it marks the transaction
confirmed. If event validation fails, the local unit update, event marker and
confirmation are rolled back together; the transaction remains submitted for
investigation or retry. A receipt with status `0` is recorded as a failed
transaction instead.

Run the temporary manual reconciliation command from `app/`:

```bash
uv run python manage.py reconcile_unit_registrations
```

The command checks only submitted `unit_registration` transactions associated
with a `UnitRegistry`, and requires the `UnitRegistered` event in each successful
receipt. It reads receipts from the configured RPC; it does not submit or sign
transactions. A transaction without a receipt remains pending. This command
can be replaced later by a Celery task that calls the same confirmation service.

## Company update view — implemented

`CompanyUpdateView` serves `/companies/<company_id>/edit/` and reuses the
existing company context rules:

- `CompanyAdministratorRequiredMixin` validates the active account, active
  membership, active company and administrator role on the server.
- A user without membership receives `404`; a member without the
  administrator role receives `403`.
- The view edits the company loaded by the membership-scoped URL context, so
  an ID from another company cannot be edited.
- The existing `CompanyForm` handles the `POST`. `active` is not exposed and
  the initial creation flow remains assisted through Django Admin.
- The detail page shows the edit link only to an active administrator. The
  link is only a UI convenience; server authorization remains mandatory.

## Unit views — implemented

The unit routes use `company_id` in every URL:

- `UnitListView` lists only units owned by the validated company.
- `UnitDetailView` filters its object lookup by that company, so a unit from
  another company returns `404`.
- `UnitCreateView` and `UnitUpdateView` require an active administrator and an
  active company. An inactive company returns `403` before the form runs.
- Create and update validate `UnitForm` and call
  `save_unit_registration` to save canonical fields and the local hash. They
  do not create a blockchain transaction or call RPC.
- The list, detail and form templates use the existing Tailwind base layout.
  The detail page shows the local registration hash, the on-chain unit ID only
  when it exists, and an explicit "awaiting blockchain registration" state
  while `chain_unit_id` is empty.
- Transaction status is rendered only when the view supplies an associated
  `blockchain_transaction` record. A missing transaction record is not shown
  as a completed or failed operation.
- The management links are hidden for regular members, but server checks remain
  mandatory.

## Add an existing user to a company — implemented

`CompanyMembershipForm`, `MemberCreateView` and the membership service implement
the first team-management flow:

- The form exposes only an existing active Django user. It does not expose a
  company field or accept company scope from submitted data.
- The company is taken from the `company_id` URL after
  `CompanyAdministratorRequiredMixin` validates the administrator's active
  membership and company.
- Existing memberships are excluded from the choices and checked again during
  validation. The database unique constraint remains the final duplicate
  protection.
- `companies.services.membership.add_company_membership` creates the active
  membership and appends an immutable `AccessChange` record with the actor,
  company and target user.
- The form does not assign a role. A later role-management step must grant
  `MembershipRole` through its own authorization rules.
- The team list shows the "Adicionar membro" action only to an active company
  administrator. Hiding the link is only a UI convenience; the server check is
  mandatory.

## Manage membership roles — implemented

`MembershipRoleForm`, `MemberRoleCreateView` and `MemberRoleRevokeView` manage
the functions assigned to a company member:

- The role form offers the five existing functions: administrator, registration,
  receiving, issuance and auditor.
- The target membership is loaded from the company-scoped URL. A membership ID
  from another company returns `404` and is never used by the form.
- The grant route requires an active administrator of the same company and
  rejects granting a function to the administrator's own membership.
- An already active function cannot be granted twice. A revoked function may be
  granted again as a new active record while the old record remains inactive.
- Revocation is a `POST` action, requires an active company administrator and
  marks the role inactive with `revoked_at`; it never deletes the role.
- Both grant and revoke append immutable `AccessChange` records containing the
  actor, member, company and before/after values.
- The member detail page shows active functions, a grant action for eligible
  members and a revoke action for each active function.

## Manage member access to units — implemented

`UnitAccessForm` and `MemberUnitAccessUpdateView` manage the four SaaS flags for
one member and one unit:

- The member detail page lists every unit belonging to the selected company and
  shows the member's current active flags.
- The administrator opens the configuration link for a unit and selects
  viewing, receiving, issuance and auditing access.
- The member and unit are loaded from the company-scoped URL. The form contains
  no company, member or unit field, so posted IDs cannot move a grant across
  companies.
- A new selection creates `UnitAccess`. Changing the selection updates the same
  record. Submitting all flags unchecked deactivates the existing record rather
  than deleting it.
- Every change is recorded in immutable `AccessChange` history. These SaaS
  flags do not change the wallet permission mask in the blockchain.
- Only an active administrator may open or submit the configuration view. An
  inactive member cannot receive a new active unit grant.

## Consult access history — implemented

`AccessHistoryListView` serves the company-scoped route
`/companies/<company_id>/history/` and is linked as **Histórico** in the
administration section of the sidebar.

- The view requires an authenticated, active company administrator through
  `CompanyAdministratorRequiredMixin`.
- The query is filtered by the validated company from the URL and uses
  `select_related` for the affected member, unit and actor. A company ID from
  another membership returns `404` before any history is exposed.
- Each entry displays the action, responsible user, affected member, optional
  unit, local date and time, a plain-language summary, readable permission
  labels and before/after states. The list is paginated at 50 entries per page.
- The database keeps structured before/after values in `JSONField`, but the
  template never exposes JSON keys such as `can_view`; it translates them to
  labels such as **Visualização**, **Concedida** and **Não concedida**.
- The page is read-only. `AccessChange` remains append-only and revoked roles,
  memberships and unit grants remain visible with their original before/after
  values.
- Membership creation, role grant/revocation and unit-access changes already
  call `companies.services.access_history.record_access_change` inside their
  transaction. A history row is created only when a real change occurs.
- The corresponding Django Admin save hooks also append entries for assisted
  creation, reactivation and revocation. Direct Admin deletes are disabled for
  memberships, roles and unit grants, and `AccessChangeAdmin` is read-only.

Operational history for participants, CPRs, receipts and issuance remains the
responsibility of their respective apps; this screen currently covers SaaS
membership and access changes.

## Company context rules — implemented

The rules are in `companies/rules/company_access.py`:

- Every company view receives a `company_id` in the URL and validates it
  against the signed-in user's active `CompanyMembership`.
- A membership from another user returns `404`; changing the numeric ID in the
  URL cannot open another company's data.
- The selected company is stored in `active_company_id` only as a UI
  convenience. The URL membership query is repeated on every request and is
  the authorization source.
- `CompanyAccessMixin` is for reads. `ActiveCompanyRequiredMixin` is for new
  operations and rejects an inactive company with `403`.
- `CompanyAdministratorRequiredMixin` extends the active-company check and
  rejects members without an active administrator role with `403`.
- The company list never picks the first membership automatically. A user
  with multiple companies must choose one, and the selected context is then
  explicit in the URL.

All future operational models must have a required company relationship, and
all company-scoped views must use one of these mixins before querying or
changing operational data.

## Role rules — implemented

Role checks are in `companies/rules/role_access.py` and use the existing
`MembershipRole` and `UnitAccess` records:

- `administrator` may manage team and company permissions.
- `registration` prepares participants and CPRs. An administrator may also
  prepare these records.
- `receiving` may operate loads only when an active `UnitAccess` grants
  `can_receive` for that unit.
- `issuance` may create and follow issuance batches only with `can_issue` for
  the unit.
- `auditor` may record audits only with `can_audit` for the designated unit.
- Every operational action requires an active company. A role by itself does
  not grant access to every unit and does not grant an on-chain permission.
- Only an administrator may grant a role, and `MembershipRole` rejects a role
  whose `granted_by` is the same user as the target membership.

Producer and creditor are participant roles, not staff membership roles. Their
own-record rules (terms, CPRs, receipts and signatures) will be implemented in
the planned `participants` and operation apps after those models exist. The
companies app must not create a staff role for them automatically.

## Unit rules — implemented

Unit checks are in `companies/rules/unit_access.py`:

- A `Unit` has one required `company` and its CNPJ is unique in the database.
- `UnitAccess` validates that the membership and unit belong to the same
  company before saving.
- A new operation first loads the unit through the selected company, checks
  that the unit is active, and then checks the active grant for the exact
  membership and permission field.
- Receiving, issuance and audit rules use this check before allowing their
  operation. A unit from another company returns `404`; a missing or inactive
  grant is denied.
- Deactivating a unit preserves its history but blocks new receiving, issuance
  and audit operations.

## Models to build

All models must include `created_at` and `updated_at` timestamps. Store timestamps in UTC and display them in the appropriate local time zone.

| Model | Purpose | Status |
| --- | --- | --- |
| `Company` | Stores legal name, trade name, normalized CNPJ and active status. | Implemented. |
| `CompanyMembership` | Links a user to a company and records whether the membership is active. A user may have memberships in multiple companies. | Implemented. |
| `MembershipRole` | Assigns one or more functions to a membership, such as administrator, registration operator, receiver, issuance operator or auditor. Records who granted the role and when it was revoked. | Implemented. |
| `Unit` | Stores a company's warehouse name, code, address, CNPJ, state registration, canonical registration hash, active status and on-chain unit ID when confirmed. | Implemented. |
| `UnitAccess` | Grants a membership access to specific units and actions: viewing, receiving, issuing and auditing. | Implemented. |
| `UnitWalletPermission` | Tracks a wallet's unit permissions and the status of the related blockchain configuration. The current field stores the public address and transaction hash as normalized references; private keys are never stored. `is_effective` is true only for a confirmed permission with a transaction hash. | Implemented; ready to connect to the planned wallet and blockchain models. |
| `AccessChange` | Records who granted, changed or revoked access, the scope, previous and new values, and any associated transaction. The record cannot be edited or deleted. | Implemented. |

Producer and creditor access is associated with participant records in the planned `participants` app. These participants do not automatically become company staff.

## Proposed routes

These paths describe the Django views and templates. Every route must require
authentication and apply the relevant company and unit permission checks on
the server. Routes marked as implemented are available in the current app.

| Route | Purpose |
| --- | --- |
| `/companies/` | List companies accessible to the signed-in user. Implemented. |
| `/companies/<company_id>/` | Show the selected company's details after membership validation. Implemented. |
| `/companies/<company_id>/edit/` | Edit company information. Implemented for active company administrators. |
| `/companies/<company_id>/members/` | List staff memberships and active roles. Implemented. |
| `/companies/<company_id>/members/new/` | Add an existing Django user to the company team. Implemented for active company administrators. |
| `/companies/<company_id>/members/<membership_id>/roles/new/` | Grant a function to a company member. Implemented for active company administrators. |
| `/companies/<company_id>/members/<membership_id>/roles/<role_id>/revoke/` | Revoke an active function with `POST`. Implemented for active company administrators. |
| `/companies/<company_id>/members/<membership_id>/` | View a membership, its roles and its unit access. Role and unit-access management are implemented. |
| `/companies/<company_id>/members/<membership_id>/units/<unit_id>/access/` | Configure SaaS access flags for one company unit. Implemented for active company administrators. |
| `/companies/<company_id>/history/` | Consult immutable membership, role and unit-access changes for the selected company. Implemented for active company administrators. |
| `/companies/<company_id>/units/` | List the company's units. Implemented. |
| `/companies/<company_id>/units/new/` | Prepare a new unit registration. Implemented for administrators. |
| `/companies/<company_id>/units/<unit_id>/` | Show a unit and its registration status. Implemented. |
| `/companies/<company_id>/units/<unit_id>/edit/` | Edit a unit locally. Implemented for administrators. |
| `/companies/<company_id>/units/<unit_id>/access/` | Manage authorized staff and wallet permissions for the unit. |

Company onboarding and contract configuration are assisted during the MVP. Decide the authorized onboarding flow before exposing a company-creation route. Route names and forms may be refined during implementation.

## Business rules

### Company and membership

- A company has a unique, normalized CNPJ. Keep its history when it is deactivated.
- A membership belongs to one user and one company; prevent duplicate memberships for the same pair. An inactive membership must not grant access.
- Assign roles per membership. A person may have different roles in different companies and more than one compatible role within a company.
- A Django superuser is a platform administrator, not automatically the on-chain owner or a business user of every company.
- Company, membership, role and unit status must be checked for every relevant request. Filtering a menu is not an authorization check. Deny cross-company access to pages, forms, downloads and endpoints.

### Units and permissions

- A unit belongs to one company. Its code must be unique within that company. Store the on-chain ID separately from the database ID, and populate it after confirmation.
- A unit's normalized CNPJ is required and unique across the SaaS, including inactive units. Form and service validation checks its digits; the database enforces its canonical format and uniqueness.
- The unit registration hash is a business rule, not a user-entered document. The SaaS normalizes `name`, `code`, `address`, `cnpj` and `state_registration`, serializes them as deterministic sorted JSON and calculates Ethereum-compatible Keccak-256. The resulting `0x`-prefixed `bytes32` value is stored in `registration_hash` and sent as `registrationHash` to `registerUnit`.
- `active`, `chain_unit_id`, database IDs and timestamps are not part of the registration hash. If a registration field changes, calculate a new hash before calling the contract's unit-update function. The original registration data remains in the database; the document-storage app is not involved in this hash rule.
- `UnitAccess` must link a membership and unit belonging to the same company. Check the role and the unit grant together before allowing an action.
- The SaaS role and the wallet's on-chain permission are separate. Granting or revoking access in the SaaS does not itself change the contract.
- The owner of the company's `UnitRegistry` must execute unit registration, auditor assignment and on-chain permission changes. Confirm the owner and transaction result before marking the change as effective. The owner of SojaCoin is checked separately.
- Follow the contract's separation rules: one wallet cannot both receive and issue in the same unit; a wallet auditing a unit cannot hold conflicting inventory permissions there. Company-wide conflicting permissions are blocked while the wallet audits any unit, including an inactive one. Apply the contract's withdrawal separation rule when withdrawal is added.
- `setPermissions` replaces the permission mask for its scope. Read the current mask and submit the complete intended mask when changing it. Track pending, confirmed and failed changes without displaying pending permissions as active.
- Keep an access history and preserve operational records. Revoking SaaS access takes effect in the application through `companies.services.access_history`; the service records the change instead of deleting the record. Revoking wallet permission requires its own confirmed blockchain transaction.

## Onboarding test coverage — implemented

The `companies` test suite covers the local company and unit onboarding flow:

- valid and invalid CNPJ values, including duplicate normalized CNPJs;
- deterministic registration hashes and hash changes after registration data changes;
- company-scoped isolation, missing memberships, inactive companies and units
  from another company;
- local-only unit creation and editing, including preservation of a confirmed
  `chain_unit_id` during edits;
- adding an existing active user to a company, duplicate-membership rejection,
  company isolation, inactive-account rejection and access-history recording;
- role grants and revocations, duplicate active-role rejection, self-grant
  rejection, cross-company protection and immutable role history;
- unit access creation, update, deactivation, reactivation, cross-company
  protection, inactive-member rejection and immutable access history;
- inactive membership, inactive role and inactive unit grant rejection before
  operational access is authorized;
- confirmation that changing SaaS unit access does not create wallet-permission
  records or blockchain transaction records;
- Django Admin membership, role and unit-access hooks, including before/after
  values and protection against deleting the source records;
- absence of `BlockchainTransaction` records during local onboarding. No RPC
  or blockchain submission is performed by these views.

## Suggested implementation order

1. **Company context checks — complete.** Company membership, role and
   unit-access rules are centralized in `companies/rules` and tested before
   any future operational form is exposed.
2. **Company and unit onboarding — complete.** The assisted company and unit
   forms, company edit flow, unit class-based views, Tailwind templates and
   local-registration tests are implemented. The local flow deliberately does
   not submit a blockchain transaction.
3. **Unit registration hash — complete.** Every enabled unit registration and
   edit path, including Django Admin if used, must call the registration-hash
   service. A locally saved unit is not yet registered on-chain.
4. **Staff and SaaS-permission workflows — complete for the planned MVP scope.**
   Adding users, role grant/revocation and unit-access management are
   implemented with immutable history and company isolation.
5. **Verified wallets and blockchain — pending.** Integrate verified wallets
   and blockchain transactions, then update `UnitWalletPermission` only after
   confirmation.

See [the MVP plan](../planejamento.md) for the complete workflow and the responsibilities of the other apps.
