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

In het wachtwoord van de database moet je `@ : / # %` URL-encoden
(`@` wordt `%40`).

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
| 502 Bad Gateway | `kasflow.service` draait niet; `journalctl -u kasflow -e` |
| Styling weg / oude pagina | service worker; hard refresh of `kasflow-v…` in `sw.js` |

De openbare git-repo is [Ahmadalaina7/kasflow-app](https://github.com/Ahmadalaina7/kasflow-app).
Voor een VPS met Docker in plaats van Plesk: [DEPLOY.md](DEPLOY.md).
