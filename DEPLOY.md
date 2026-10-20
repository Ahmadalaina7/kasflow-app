# Kasflow naar productie

Deze gids zet Kasflow op één Europese server met PostgreSQL, HTTPS en dagelijkse
back-ups. Alles wat je nodig hebt: een VPS met Docker, een domein en een
Cloud86-mailbox voor de uitgaande e-mail.

> **Nog niet op een echte server gebouwd.** Deze gids en de configuratie zijn
> geschreven en gecontroleerd (YAML parseert, paden, poorten en de
> healthcheck-URL kloppen, de app start lokaal met dezelfde variabelen), maar de
> Docker-image is nog niet op een VPS gebouwd en gestart. Reken op één
> testronde op een testserver — inclusief de herstelprocedure — voordat je live
> gaat.

> **Cloud86 shared hosting kan Kasflow niet draaien.** Hun webhostingpakketten
> zijn PHP-only: je kunt er geen Python-proces, `uvicorn` of PostgreSQL
> draaien. Gebruik een Cloud86 **VPS** (met Docker), **Plesk met Python 3.11+
> en PostgreSQL** ([PLESK.md](PLESK.md)), of een andere EU-server. De
> Cloud86-mailbox gebruik je wél: e-mail versturen gaat via SMTP vanaf elke host.

## 0. Overzicht

| Onderdeel | Waar het draait | Waarom |
| --- | --- | --- |
| FastAPI-app | Docker-container `app` | Luistert alleen op 127.0.0.1:8010 |
| PostgreSQL 17 | Docker-container `db` | Gegevens, geen open poort naar buiten |
| Bonnetjes | Docker-volume `uploads` | Buiten de webmap opgeslagen |
| HTTPS | Docker-container `caddy` | Automatisch Let's Encrypt-certificaat |
| E-mail | Cloud86-mailbox via SMTP | Verificatie, wachtwoordreset, facturen |

## 1. Server klaarzetten

```bash
# Op de VPS, als root
adduser kasflow && usermod -aG sudo kasflow
# Daarna inloggen als kasflow

sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # daarna opnieuw inloggen
```

Kies een VPS met minimaal 2 GB RAM en 20 GB schijf. Zet de tijdzone goed:

```bash
sudo timedatectl set-timezone Europe/Amsterdam
```

## 2. Code en configuratie

```bash
sudo mkdir -p /srv && sudo chown $USER /srv
git clone <jouw-repo-url> /srv/kasflow   # of: rsync -a ./ kasflow@server:/srv/kasflow/
cd /srv/kasflow

cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # KASFLOW_SECRET_KEY
openssl rand -base64 32                                         # POSTGRES_PASSWORD
nano .env
chmod 600 .env
```

Vul in `.env`: `KASFLOW_SECRET_KEY`, `KASFLOW_PUBLIC_URL`, `KASFLOW_DOMAIN`,
`KASFLOW_ALLOWED_HOSTS`, `POSTGRES_PASSWORD` en de `KASFLOW_SMTP_*`-waarden.

De app controleert de configuratie bij het starten: zonder sleutel van minimaal
32 tekens, met een `http://`-URL of met SQLite stopt hij met een duidelijke
foutmelding.

## 3. Starten

```bash
docker compose up -d --build
docker compose ps
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8010/api/health').read().decode())"
```

Verwacht: `{"status":"ok","version":"2.0.0","database":"ok","mail":"smtp","env":"production"}`.

Zet daarna bij je DNS de A-records van `app.example.nl` (en `www`) op het
IP-adres van de server. Caddy vraagt dan automatisch een certificaat aan. Test:

```bash
curl -sS https://app.example.nl/api/health
```

## 4. E-mail via Cloud86

In het Cloud86-paneel:

1. Maak een mailbox aan, bijvoorbeeld `noreply@example.nl`.
2. Zet **SPF** op `v=spf1 include:cloud86.nl ~all` (of de door Cloud86
   aangegeven include) en zet **DKIM** aan; Cloud86 toont een DNS-record dat je
   bij je domeinregistrar plaatst.
3. Zet een DMARC-record: `v=DMARC1; p=quarantine; rua=mailto:dmarc@example.nl`.
4. Vul in `.env`: `KASFLOW_SMTP_HOST=mail.example.nl`, `KASFLOW_SMTP_PORT=587`,
   `KASFLOW_SMTP_SECURITY=starttls`, `KASFLOW_SMTP_USER=noreply@example.nl` en
   `KASFLOW_SMTP_PASSWORD=<mailboxwachtwoord>`.

Herstart met `docker compose up -d`. Zodra SMTP is ingesteld, is
e-mailverificatie verplicht bij registratie en kun je facturen naar klanten
mailen. Let op het verzendlimiet van Cloud86 (ongeveer 250 mails per uur) — ruim
voldoende voor facturen, maar deel geen bulkmail via deze mailbox.

Controleer de aflevering:

```bash
docker compose logs app | grep -i mail
```

## 5. Back-ups

De app bewaart niets buiten de database en het uploadvolume, dus een back-up
bestaat uit twee bestanden. Zet dit als `/srv/kasflow/backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /srv/kasflow
STAMP=$(date +%Y-%m-%d)
DEST=/srv/backups
mkdir -p "$DEST"

# Database
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-kasflow}" "${POSTGRES_DB:-kasflow}" \
  | gzip > "$DEST/db-$STAMP.sql.gz"

# Bonnetjes (via de app-container, zodat de volumennaam niet uitmaakt)
docker compose exec -T app tar czf - -C /app/backend/uploads . \
  > "$DEST/uploads-$STAMP.tar.gz"

# Dertig dagen bewaren
find "$DEST" -type f -mtime +30 -delete

# Zichtbare controle in het back-uplog: lege bestanden betekenen dat een
# container niet draaide en dat je back-up niets bevat.
ls -lh "$DEST/db-$STAMP.sql.gz" "$DEST/uploads-$STAMP.tar.gz"
```

```bash
chmod +x /srv/kasflow/backup.sh
crontab -e
# dagelijks om 03:15, plus een wekelijkse kopie naar een externe locatie
15 3 * * * /srv/kasflow/backup.sh >> /var/log/kasflow-backup.log 2>&1
```

Kopieer `db-*.sql.gz` en `uploads-*.tar.gz` wekelijks naar een andere locatie
(bijvoorbeeld Cloud86-opslag of een tweede server). Een back-up die alleen op
dezelfde server staat, is geen back-up.

### Herstellen

Op een schone server met dezelfde `.env`:

```bash
cd /srv/kasflow

# 1. Database terugzetten (de app is nog niet nodig)
docker compose up -d db
gunzip -c /srv/backups/db-2026-10-19.sql.gz \
  | docker compose exec -T db psql -U "${POSTGRES_USER:-kasflow}" "${POSTGRES_DB:-kasflow}"

# 2. App starten en de bonnetjes terugzetten
docker compose up -d
docker compose exec -T app sh -c "rm -rf /app/backend/uploads/*"
docker compose exec -T app tar xzf - -C /app/backend/uploads < /srv/backups/uploads-2026-10-19.tar.gz

# 3. Controleren
curl -sS https://app.example.nl/api/health
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8010/api/invoices').status)"
```

Test dit één keer op een testserver: een herstelprocedure die nooit is
uitgevoerd, werkt zelden.

## 6. Bijwerken en terugrollen

```bash
cd /srv/kasflow
./backup.sh                      # altijd eerst
git fetch --all && git checkout <nieuwe-tag>
docker compose up -d --build
docker compose logs -f app       # let op migratiemeldingen en 500's
```

Databasemigraties zijn additief en idempotent, dus terugrollen naar de vorige
versie is veilig: `git checkout <oude-tag> && docker compose up -d --build`.
Verwijder nooit kolommen in dezelfde uitrol waarin je ze niet meer gebruikt.

## 7. Beheer en controle

```bash
docker compose logs -f app            # app-log (JSON-achtige regels, NL)
docker compose logs caddy | tail -50  # HTTPS en verzoeken
docker compose exec db psql -U kasflow -c "\dt"
docker compose run --rm app python -m pytest -q   # tests in de image
```

Wat je periodiek wilt controleren:

- `/api/health` geeft `status: ok` en `database: ok`.
- De schijf blijft onder 80% (`df -h`), anders lopen back-ups en uploads vast.
- `docker compose ps` toont alle containers als `healthy`.
- Er staan geen onverwachte fouten in `docker compose logs app`.

## 8. Groeien

- **Meerdere workers**: zet `WEB_CONCURRENCY=2` of hoger. De rate limiter en de
  uploadlimieten zijn dan per worker; voor harde limieten zet je een gedeelde
  teller (Redis) voor de inlog- en verzendroutes.
- **Objectopslag**: verhuis bonnetjes naar S3-compatibele opslag binnen de EU
  zodra het uploadvolume groter wordt dan de schijf.
- **Monitoring**: stuur `/api/health` elke minuut naar een uptime-monitor en
  laat alarm slaan op `status != ok`.
- **Staging**: draai dezelfde Compose-stack op een tweede host met een eigen
  `.env`, database en `KASFLOW_PUBLIC_URL`, zodat je migraties eerst daar test.
