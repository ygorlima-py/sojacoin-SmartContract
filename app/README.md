# Frontend development

Run these commands from the `app` directory. Install the JavaScript dependencies
with `npm ci`, and install the Python dependencies with `uv sync`.

Generate the Tailwind CSS once:

```bash
npm run css:build
```

During frontend development, keep the following command running in a separate
terminal to rebuild the CSS when templates change:

```bash
npm run css:watch
```

The source is `static/src/input.css`; the generated file is
`static/css/app.css`. Tailwind scans the Django application directory, including
templates inside individual apps. The generated CSS is not committed, so run
the build after installing dependencies and before deploying.

With `DEBUG=True`, Django's development server serves the static files. For
deployment, build the CSS first, then gather all static files:

```bash
npm run css:build
uv run python manage.py collectstatic --noinput
```

The collected output is `staticfiles/`. Configure the deployment's web server
or static-file service to serve that directory at `STATIC_URL`. Uploaded user
documents are separate from these static files.

## Interface languages

The interface supports Brazilian Portuguese (`pt-br`, the default) and English
(`en`). Use the language selector in the page header; the choice is stored in a
browser cookie and does not change URLs or account data. Public participant
pages use the same selector. Uploaded PDFs and blockchain values remain in the
form and language in which they were created.

When adding user-facing text, mark Django templates with `{% translate %}` and
Python strings with `gettext` or `gettext_lazy`. Mark browser messages with
`gettext()` from Django's JavaScript catalog. Keep model values, permissions,
transaction arguments, hashes, and stored documents unchanged.

From this directory, refresh the catalogs with Django's GNU gettext commands:

```bash
uv run django-admin makemessages -l en -d django
uv run django-admin makemessages -l pt_BR -d django
uv run django-admin makemessages -l en -d djangojs
uv run django-admin makemessages -l pt_BR -d djangojs
uv run django-admin compilemessages
```

Review the `msgstr` values in `locale/`, then compile again. GNU gettext must
be installed for `makemessages` and `compilemessages`.

## Django apps

- `accounts`: authentication, profiles and wallet verification.
- `companies`: companies, units, staff access and immutable access history.
- `participants`: company-scoped participant records, exact terms, public acceptance links
  and blockchain registration. See [`participants/README.md`](participants/README.md).
- `receipts`: company-scoped local load records, weighing data, NF-e details and private PDFs.
  See [`receipts/README.md`](receipts/README.md).
- `issuance_batches`: receipt-backed batch creation, atomic participant approval
  and token issuance, transaction recovery and cancellation. See
  [`issuance_batches/README.md`](issuance_batches/README.md).
- `blockchain`: networks, contract instances, transaction lifecycle and
  idempotent processed-event markers. See [`blockchain/README.md`](blockchain/README.md).
