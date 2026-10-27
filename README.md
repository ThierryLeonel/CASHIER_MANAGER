# Cashier Manager

Cashier management web application for an Orange Cameroon distribution partner. It records supplies, destockage, collections, cash-manager exchanges, UVE→ER conversions, expenses, internal transfers, daily closings, discrepancies and corrections in a double-entry ledger. It provides EN/FR screens, A4 receipts and Excel/PDF reports.

Design, accounting rules, posting and role matrices: **[DESIGN.md](DESIGN.md)**.
Hosting on Laravel Cloud: **[DEPLOY_LARAVEL_CLOUD.md](DEPLOY_LARAVEL_CLOUD.md)**.

## Requirements

* Python 3.11+ (tested on 3.13)
* PostgreSQL 14+ (tested on 16). Required: the ledger relies on PostgreSQL triggers, sequences and row locks.

```bash
pip install -r requirements.txt
```

## Setup

```bash
# 1. Database
sudo -u postgres psql -c "CREATE ROLE cashier LOGIN PASSWORD 'choose-a-password';"
sudo -u postgres psql -c "CREATE DATABASE cashier OWNER cashier;"

# 2. Configuration (secrets stay out of source code)
cp .env.example .env        # set DJANGO_SECRET_KEY, DB_PASSWORD, hosts, company name
python -c "import secrets; print(secrets.token_urlsafe(50))"   # use as DJANGO_SECRET_KEY

# 3. Schema, translations, static files
python manage.py migrate
python manage.py build_translations
python manage.py collectstatic --noinput

# 4a. Production: first super administrator, then configure in the app
python manage.py create_superadmin admin --name "Super admin"
#   /admin/ : zones → users (cashiers with subzone) → SIMs (custodian, master flag)
#             → drawers → account grants (e.g. Fako 2 → Fako master ER) → banks, cash managers
#   /setup/conversion-rate/ : set 4 %
#   /setup/opening-balances/ : launch balances (once per account)

# 4b. Demo instead: clearly marked [SAMPLE] data (empty database only)
python manage.py load_sample_data --activity     # password for all users: Sample-Pass-2026!
```

Run locally with `python manage.py runserver` and `DJANGO_DEBUG=1`. In production run `gunicorn config.wsgi` behind HTTPS (nginx or similar). Set `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`. Session and CSRF cookies are secure-only when DEBUG is off.

Sample users: `admin`, `manager`, `finance`, `cashier.fako1`, `cashier.fako2`, `cashier.meme`, `agent.ndive`, `agent.ekane`.

## Tests

```bash
python manage.py test core       # 57 tests, real PostgreSQL (triggers active)
python manage.py ledger_check    # SQL integrity check of a live database
```

The tests cover every scenario in the brief:

* supplies and mixed payment methods;
* shared agent balances across cashiers;
* destockage and UVE repayment without double counting;
* direct POS supply and full responsibility transfer, including refusal when the debt is partly paid;
* excess payment followed by supply;
* cash-manager legs at different times;
* conversion rounding and rate history;
* ER master-SIM restriction;
* expenses and internal transfers;
* closing, carry-forward and next-business-day entries;
* discrepancy investigation and resolution;
* corrections, including closed days and overspend protection;
* concurrent spending from one drawer and concurrent duplicate submissions (real threads);
* append-only and balance triggers;
* agent data isolation and prohibited actions over HTTP;
* receipts and CORRECTED stamp checked inside the PDF text;
* Excel and PDF exports honouring the same filters as the screen;
* French catalogue completeness.

## Backup and restore

```bash
# nightly (cron), keep off-server copies
pg_dump -Fc -h localhost -U cashier cashier > /backups/cashier-$(date +%F).dump

# restore into an empty database
createdb -h localhost -U postgres -O cashier cashier_restored
pg_restore -h localhost -U cashier -d cashier_restored --no-owner /backups/cashier-2026-10-27.dump
DB_NAME=cashier_restored python manage.py ledger_check     # verify before switching
```

`pg_dump` includes the triggers and sequences, so receipt numbering continues correctly after a restore. Back up `.env` separately and securely.

## Translations

The French text lives in `core/i18n_fr.py`. `python manage.py build_translations` writes the `.po`/`.mo` files and fails if any string lacks French. GNU gettext is not needed.

## Remaining limitations

* The six accounting defaults in DESIGN.md §2 await business confirmation; each is isolated in one function.
* Bank accounts are not counted at cashier closing. They are reconciled through the reconciliation and account-movement reports.
* No automatic e-mail/SMS of receipts. Receipts are printed or saved as PDF.
* Login throttling uses an in-memory cache unless `REDIS_URL` is set; attach Redis/Valkey when running several instances.
* Reports run synchronously. They are fine for the expected volume (thousands of transactions per month); very long "overall" exports may need paging later.
* Amounts are whole FCFA (XAF has no sub-unit in use). The column type would need a migration to store fractions.
