# Kasflow op Plesk

Kasflow is een Python-app (FastAPI), geen PHP-site. Op Plesk draait hij achter
nginx, met PostgreSQL uit **Plesk > Databases** en HTTPS via Let's Encrypt in
Plesk. Shared hosting zonder SSH en zonder Python 3.11+ is niet genoeg.

> **Aanbevolen: methode A** (nginx-proxy + gunicorn). Methode B (Passenger) is
> een valkuil als Plesk geen systemd toestaat.

Python **3.11 of nieuwer**. De app weigert te starten in productie zonder
`https://`, zonder geheime sleutel van 32+ tekens, of met SQLite.

## 0. In Plesk klaarzetten

1. Domein aanmaken, bijvoorbeeld `app.jouwdomein.nl`.
2. **SSL/TLS-certificaten** > Let's Encrypt, ook voor www als je die gebruikt.
3. **Databases** > database toevoegen, type **PostgreSQL**.
   Noteer: databasenaam, gebruiker, wachtwoord. Host is vrijwel altijd `localhost`.
4. **Webhostingtoegang** > SSH inschakelen voor deze gebruiker (jailshell mag).
5. Noteer de **systeemgebruiker** van het abonnement (bijvoorbeeld `jouwdomein`).

## 1. Code op de server

SSH naar de server als de Plesk-gebruiker (niet als root, behalve bij systemd):

```bash
cd /var/www/vhosts/app.jouwdomein.nl
git clone https://github.com/Ahmadalaina7/kasflow-app.git kasflow
cd kasflow
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
mkdir -p backend/uploads
chmod 700 backend/uploads
```

Python 3.11 ontbreekt? Vraag de host om het, of installeer als root
`plesk installer` / het pakket `python3.11`. Controleer met `python3 --version`.

## 2. Configuratie

```bash
cp plesk/env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
nano .env
chmod 600 .env
```

Vul minstens:

| Variabele | Waarde |
| --- | --- |
| `KASFLOW_SECRET_KEY` | de zojuist gegenereerde sleutel |
| `KASFLOW_PUBLIC_URL` | `https://app.jouwdomein.nl` |
| `KASFLOW_ALLOWED_HOSTS` | `app.jouwdomein.nl` (en `www.` als die meedoet) |
| `KASFLOW_DATABASE_URL` | `postgresql+psycopg://GEBRUIKER:WACHTWOORD@localhost:5432/DATABASENAAM` |
| `KASFLOW_UPLOAD_DIR` | het absolute pad naar `kasflow/backend/uploads` |
| `KASFLOW_SMTP_*` | je mailbox (Cloud86 of de Plesk-mailserver) |
| `KASFLOW_MOLLIE_API_KEY` | Mollie-sleutel voor iDEAL en de maandelijkse incasso (mag leeg blijven) |

In het wachtwoord van de database moet je `@ : / # %` URL-encoden
(`@` wordt `%40`).

### Betalingen (Mollie)

Zonder `KASFLOW_MOLLIE_API_KEY` start Kasflow gewoon, maar kan niemand een
abonnement afsluiten: de proefperiode van 14 dagen loopt door en Instellingen
zegt wat er ontbreekt. Met een sleutel int Mollie per maand via SEPA Direct
Debit, op basis van een iDEAL-betaling die de klant zelf goedkeurt.

Wat op de server goed moet staan:

1. Mollie stuurt de webhook naar `KASFLOW_PUBLIC_URL/api/billing/webhook`,
   dus `KASFLOW_PUBLIC_URL` moet het echte `https://`-adres zijn en van buiten
   bereikbaar (geen `localhost`, geen IP met een self-signed certificaat).
2. De webhook is een POST zonder `Origin`-header: nginx hoeft hier niets voor
   open te zetten, het bestaande `location /` blok is genoeg.
3. Zet in je Mollie-profiel **SEPA Direct Debit** aan, anders weigert Mollie het
   mandaat voor de volgende maanden.
4. Begin met een `test_`-sleutel, doe één betaling in testmodus, en zet daarna
   `live_` in `.env` met `sudo systemctl restart kasflow`.

Het abonnement zelf staat in de tabel `subscriptions` en gaat mee in
`plesk/backup.sh`. De webhook is het vangnet: de terugkeerpagina controleert de
betaling ook, dus een gemiste melding blokkeert niemand permanent.

## 3. Methode A — nginx-proxy (aanbevolen)

### 3a. systemd-service

Als root (of via de host):

```bash
# Pas User, paden en domein aan in het bestand, daarna:
sudo cp /var/www/vhosts/app.jouwdomein.nl/kasflow/plesk/kasflow.service \
        /etc/systemd/system/kasflow.service
sudo nano /etc/systemd/system/kasflow.service
sudo systemctl daemon-reload
sudo systemctl enable --now kasflow
sudo systemctl status kasflow
```

`User=` is de Plesk-systeemgebruiker van het abonnement. De app luistert alleen
op `127.0.0.1:8010` (niet bereikbaar vanaf internet).

```bash
curl -sS http://127.0.0.1:8010/api/health
```

Verwacht `"status":"ok"` en `"database":"ok"`. `"mail":"log"` betekent dat SMTP
nog niet is ingesteld; de app start wél.

### 3b. Plesk-nginx

**Domein > Apache & nginx-instellingen:**

1. Haal **Proxymodus** uit (nginx praat dan niet meer met Apache).
2. Plak de inhoud van [`plesk/nginx-additional.conf`](plesk/nginx-additional.conf)
   bij **Aanvullende nginx-directives**.
3. Zet bij **Aanvullende directives voor HTTP** niets dat `location /` overschrijft.
4. Opslaan. Plesk herlaadt nginx.

Open `https://app.jouwdomein.nl/api/health` in de browser.

## 4. Methode B — Passenger (als systemd niet mag)

1. Kopieer [`plesk/htaccess`](plesk/htaccess) naar `.htaccess` in de projectroot.
2. Zet `PassengerPython` in dat bestand op jouw `venv/bin/python`.
3. **Hostinginstellingen**: documentroot op de map `kasflow` (niet `httpdocs`).
4. Python-ondersteuning aan, versie 3.11+.
5. Proxymodus mag aan blijven; Apache/Passenger handelt `/` af.

Passenger praat WSGI; `passenger_wsgi.py` in de projectroot vertaalt FastAPI.
Uploads en lange requests zijn hier minder betrouwbaar dan bij methode A.

### Staat er LiteSpeed in plaats van nginx?

Sommige Plesk-abonnementen draaien LiteSpeed. Herken het aan de `Server:
LiteSpeed`-regel in de antwoordheaders, en aan het ontbreken van **Aanvullende
nginx-directives** bij Apache & nginx-instellingen. Dan werkt methode A niet.

Controleer eerst of **Python** in het Plesk-menu bij het domein staat:

- **Wel Python**: gebruik methode B (Passenger + `.htaccess`). Zet de
documentroot op de projectroot, niet op `httpdocs`.
- **Geen Python**: dit abonnement kan geen Python-proces draaien. Kies dan een
  VPS met Docker ([DEPLOY.md](DEPLOY.md)) en laat het domein daarheen wijzen.

Uploaden van alleen de HTML-bestanden is nooit genoeg: dan laadt de pagina wel,
maar antwoordt `/api/health` met een 404-pagina van de webserver en mislukken
inloggen en registreren. Test na elke deploy `https://jouwdomein.nl/api/health`:
alleen `{"status":"ok"}` betekent dat de app echt draait.

## 5. E-mail

Zelfde mailbox als in [DEPLOY.md](DEPLOY.md) §4. Zonder SMTP start Kasflow, maar
facturen en wachtwoordmails gaan niet naar klanten (alleen naar het serverlog).

## 6. Back-ups

```bash
chmod +x plesk/backup.sh
crontab -e
# 15 3 * * * /var/www/vhosts/app.jouwdomein.nl/kasflow/plesk/backup.sh >> $HOME/kasflow-backup.log 2>&1
```

Kopieer `~/backups/db-*.sql.gz` en `uploads-*.tar.gz` wekelijks naar een andere
locatie. Plesk’s eigen back-up van het abonnement meenemen mag extra, niet in
plaats daarvan.

### Herstellen

```bash
gunzip -c ~/backups/db-2026-10-20.sql.gz | psql "$KASFLOW_DATABASE_URL"
tar xzf ~/backups/uploads-2026-10-20.tar.gz -C backend/uploads
sudo systemctl restart kasflow   # of in Plesk de PHP/Python-app herstarten
```

(`psql` leest `postgresql://...`; als dat hapert: `psql -h localhost -U ... -d ...`.)

## 7. Bijwerken

```bash
cd /var/www/vhosts/app.jouwdomein.nl/kasflow
./plesk/backup.sh
git pull
source venv/bin/activate
pip install -r backend/requirements.txt
sudo systemctl restart kasflow
curl -sS https://app.jouwdomein.nl/api/health
```

Migraties draaien bij het starten en zijn additief.

## 8. Wat vaak misgaat

| Symptoom | Oorzaak |
| --- | --- |
| `Invalid production configuration` | `.env` mist sleutel, `https://` of PostgreSQL |
| 403 TrustedHost | `KASFLOW_ALLOWED_HOSTS` komt niet overeen met het domein |
| 413 bij bon-upload | `client_max_body_size` in nginx te laag |
| Facturen komen niet aan | SMTP niet gezet, of poort 587 geblokkeerd |
| "Betalen is op deze server nog niet ingesteld" | `KASFLOW_MOLLIE_API_KEY` leeg; daarna service herstarten |
| Betaling blijft op "wacht op betaling" | webhook onbereikbaar: `KASFLOW_PUBLIC_URL` moet publiek `https://` zijn |
| Pagina's laden, maar `/api/health` geeft 404 | de webserver serveert alleen statische bestanden; de Python-app draait niet |
| Registreren zegt "geen geldig antwoord (fout 404)" | zelfde oorzaak: geen Python-proces op deze hosting |
| 502 Bad Gateway | `kasflow.service` draait niet; `journalctl -u kasflow -e` |
| Styling weg / oude pagina | service worker; hard refresh of `kasflow-v…` in `sw.js` |

De openbare git-repo is [Ahmadalaina7/kasflow-app](https://github.com/Ahmadalaina7/kasflow-app).
Voor een VPS met Docker in plaats van Plesk: [DEPLOY.md](DEPLOY.md).
