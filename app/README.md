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

## Django apps

- `accounts`: authentication, profiles and wallet verification.
- `companies`: companies, units, staff access and immutable access history.
- `participants`: company-scoped participant records, exact terms, public acceptance links
  and blockchain registration. See [`participants/README.md`](participants/README.md).
- `receipts`: company-scoped local load records, weighing data, NF-e details and private PDFs.
  See [`receipts/README.md`](receipts/README.md).
- `blockchain`: networks, contract instances, transaction lifecycle and
  idempotent processed-event markers. See [`blockchain/README.md`](blockchain/README.md).
