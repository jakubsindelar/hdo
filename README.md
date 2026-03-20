# HDO přehled

Jednoduchá lokální webová aplikace nad SQLite, která:

- importuje HDO časy ze starého `.xls` souboru,
- ukládá je do vlastní databáze,
- zobrazuje aktuální stav nočního tarifu,
- ukazuje intervaly na následujících 7 dní.

## Spuštění

```bash
python3 app.py
```

Aplikace se spustí na `http://127.0.0.1:8000`.

Při prvním startu automaticky naimportuje výchozí soubor:

```text
/Users/fanatik/Downloads/aktualni-program-hdo-ke-stazeni-3.xls
```

Ruční import:

```bash
python3 app.py import --source /cesta/k/souboru.xls
```

## Docker Compose

Připravené spuštění v Dockeru:

Porty nastavíte v `.env`:

```dotenv
HDO_HOST_PORT_PROD=38417
HDO_HOST_PORT_DEV=38416
```

Produkce:

```bash
docker compose up --build -d
```

Aplikace bude dostupná na:

```text
http://127.0.0.1:38417
```

Kontejner běží na interním portu `8000`.

Development (host port o 1 menší):

```bash
docker compose -f docker-compose.dev.yml up --build -d
```

Aplikace bude dostupná na:

```text
http://127.0.0.1:38416
```

Kontejner běží i v developmentu na interním portu `8000`.

Compose mapuje:

- SQLite data do `./data`
- zdrojový Excel z `/Users/fanatik/Downloads/aktualni-program-hdo-ke-stazeni-3.xls`

K dispozici je i šablona `.env.example`.

Ochrana proti indexaci:

- aplikace vrací `X-Robots-Tag: noindex, nofollow`
- HTML obsahuje `meta robots`
- `/robots.txt` zakazuje procházení celého webu

## SSO a uživatelé

Aplikace podporuje SSO přes OIDC (Google, Apple) a interní správu uživatelů.

Základní proměnné:

```dotenv
HDO_SECRET_KEY=nahodne-dlouhe-tajne-heslo
HDO_AUTH_ENABLED=1
HDO_EXTERNAL_BASE_URL=https://hdo.example.com
```

Google:

```dotenv
HDO_OAUTH_GOOGLE_CLIENT_ID=...
HDO_OAUTH_GOOGLE_CLIENT_SECRET=...
```

Do Google OAuth klienta dej callback:

```text
https://hdo.example.com/auth/google/callback
```

Apple:

```dotenv
HDO_OAUTH_APPLE_CLIENT_ID=...
HDO_OAUTH_APPLE_CLIENT_SECRET=...
```

Po prvním úspěšném přihlášení vznikne uživatel v databázi. První uživatel je automaticky admin.

Zastavení:

```bash
docker compose down
```
