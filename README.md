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

```bash
docker compose up --build -d
```

Aplikace bude dostupná na:

```text
http://127.0.0.1:38417
```

Compose mapuje:

- SQLite data do `./data`
- zdrojový Excel z `/Users/fanatik/Downloads/aktualni-program-hdo-ke-stazeni-3.xls`

Ochrana proti indexaci:

- aplikace vrací `X-Robots-Tag: noindex, nofollow`
- HTML obsahuje `meta robots`
- `/robots.txt` zakazuje procházení celého webu

Zastavení:

```bash
docker compose down
```
