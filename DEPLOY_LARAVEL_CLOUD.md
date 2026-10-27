# Deploying on Laravel Cloud (from GitHub)

Laravel Cloud runs Django apps natively. This project already reads the variables Cloud injects (`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, `REDIS_URL`, `STATIC_ROOT`, `STATIC_URL`).

## 1. Create the application

1. In Laravel Cloud: **New application → GitHub →** choose this repository, branch `main`, region closest to Cameroon (e.g. Europe – Frankfurt or Paris).
2. Cloud detects Python/Django from `requirements.txt` and `manage.py`.

## 2. Attach a database

On the environment's infrastructure canvas, add a **PostgreSQL** database (Serverless Postgres is fine). Cloud injects `DATABASE_URL`.

* PostgreSQL is required. The ledger uses triggers, sequences and row locks, so MySQL will not work.
* Optional: add a **Redis/Valkey cache**. Cloud injects `REDIS_URL`, and login lockout is then shared across instances.

## 3. Commands

**Build commands**
```sh
python manage.py collectstatic --noinput
```
(French translations are already compiled in `locale/`, so no build step is needed for them.)

**Deploy commands** (run before each release goes live; a failure cancels it)
```sh
python manage.py migrate --noinput
```

**Start command** (Cloud pre-fills something similar; keep IPv6 + `$PORT`)
```sh
gunicorn config.wsgi --bind [::]:$PORT --graceful-timeout 25
```

## 4. Environment variables to add

| Variable | Value |
|---|---|
| `COMPANY_NAME` | Your company name (printed on receipts) |
| `COMPANY_ADDRESS` | Address line for receipts |

`SECRET_KEY`, `DEBUG=false`, hosts and the database URL are provided by Cloud; keep `DEBUG` false.

## 5. First super administrator (once)

1. Temporarily add the environment variable `DJANGO_SUPERADMIN_PASSWORD` with a strong password (10+ characters).
2. Use Cloud's command runner:
   ```sh
   python manage.py create_superadmin admin --name "Super admin"
   ```
3. **Delete** `DJANGO_SUPERADMIN_PASSWORD` from the environment.
4. Log in at `https://<your-app>.laravel.cloud/` and configure the following in `/admin/`:
   1. Zones.
   2. Cashier users.
   3. SIMs (custodian, master flag).
   4. Drawers.
   5. Account grants (Fako 2 → Fako master ER wallet).
   6. Bank account and cash managers.

   Then set the conversion rate at `/setup/conversion-rate/` and enter launch balances at `/setup/opening-balances/`.

For a demo environment instead, run `python manage.py load_sample_data --activity` on an empty database. All users get the password `Sample-Pass-2026!`. Never do this in production.

## 6. Checks after deploying

```sh
python manage.py ledger_check     # all lines should say "ok"
```

## 7. Backups

Use the database's backup / point-in-time restore feature in Laravel Cloud. Also take an occasional off-platform copy with `pg_dump -Fc "$DATABASE_URL" > cashier.dump` from a machine that can reach the database. Restore steps are in README.md.

## 8. Updating

Push to `main`. Cloud builds, runs `migrate`, then switches traffic. Ledger history is append-only, so deployments never rewrite financial data.
