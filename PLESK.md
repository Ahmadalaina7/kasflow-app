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

Open `https://app.jouwdomein.nl/api/health` in de browser. Verwacht JSON
(`"status":"ok"`). Een HTML-404 betekent dat nginx `/api` niet doorstuurt:
controleer of het blok hierboven écht in de aanvullende directives staat en
of proxymodus uit staat.

Zonder dat nginx-blok vangt `.htaccess` + `proxy.php` `/api` op Apache en
LiteSpeed, mits gunicorn op `127.0.0.1:8010` luistert. Op nginx-only doet
`.htaccess` niets.

## 4. Methode B — Passenger (als systemd niet mag)

Alleen op een domein dat door **Apache of nginx** wordt geserveerd; op LiteSpeed
werkt deze route niet (zie hierboven).

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
nginx-directives** bij de webserverinstellingen. Dan werkt methode A zoals
hierboven beschreven niet, en methode B (Passenger) meestal ook niet: de
Python-optie van Plesk gebruikt Phusion Passenger en is gedocumenteerd voor
Apache en nginx. LiteSpeed ondersteunt Passenger alleen voor CloudLinux
Selectors, kent de directive `PassengerEnabled` niet en accepteert de meeste
`Passenger*`-directives niet in `.htaccess`.

Op LiteSpeed werkt daarom dezelfde opzet als methode A, met een andere
webserverconfiguratie:

1. Start de app als service op `127.0.0.1:8010` (methode A, stap 3a). Heb je
   geen root, dan kan dit niet en is een VPS met Docker de route.
2. Plak [`plesk/litespeed-proxy.conf`](plesk/litespeed-proxy.conf) bij
   **Aanvullende directives voor HTTPS** van het domein. Daarin staat een
   `RewriteRule ... [P,L]` naar de app, de LiteSpeed-vorm van reverse proxy.
3. Zet in `.env` `KASFLOW_ALLOWED_HOSTS` op je domein. Blijkt de proxy de
   `Host`-header te vervangen, voeg dan `127.0.0.1:8010` toe; anders geeft de
   app een 400 Invalid host.
4. Test `https://jouwdomein.nl/api/health`. Alleen `{"status":"ok"}` betekent
   dat de app echt draait.

Uploaden van alleen de HTML-bestanden is nooit genoeg: dan laadt de pagina wel,
maar antwoordt `/api/...` met een 404-pagina van de webserver en mislukken
inloggen en registreren. Doe deze controle na elke deploy.


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

Alleen `git pull` is niet genoeg: gunicorn blijft de oude Python-code en de
oude `signup.html` in het geheugen houden tot je de service herstart.

```bash
cd /var/www/vhosts/app.jouwdomein.nl/kasflow
./plesk/backup.sh
git pull
source venv/bin/activate
pip install -r backend/requirements.txt
# Verplicht: anders blijft de oude KVK-check (zonder Bedrijfsdata) draaien.
sudo systemctl restart kasflow
curl -sS https://app.jouwdomein.nl/api/health
curl -sS "https://app.jouwdomein.nl/api/kvk/lookup?kvk_number=42101005"
```

`/api/health` moet `"kvk":"bedrijfsdata"` bevatten. Ontbreekt dat veld, dan
draait nog de oude worker — herstart opnieuw en controleer
`WorkingDirectory` in `/etc/systemd/system/kasflow.service`.

De lookup van `42101005` moet `"name":"webnestiQ"` teruggeven. Krijg je HTML,
`404` of `"Not Found"`, dan proxy’t de webserver `/api` niet naar gunicorn
(zie §8).

Zet `KASFLOW_KVK_API_KEY` in `.env` **niet** op een placeholder. Een ongeldige
sleutel dwingt de officiële KVK-API af; die weigert echte eenmanszaken zonder
betaalde key.

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
| KVK-nummer “konden we niet controleren” na deploy | service niet herstart, of `KASFLOW_KVK_API_KEY` in `.env` gevuld met een nep-sleutel; `curl` de lookup hierboven |
| `/api/health` mist `"kvk"` | gunicorn draait nog de oude code; `sudo systemctl restart kasflow` |
| Registreren zegt "geen geldig antwoord (fout 404)" | zelfde oorzaak: geen Python-proces op deze hosting |
| 502 Bad Gateway | `kasflow.service` draait niet; `journalctl -u kasflow -e` |
| Styling weg / oude pagina | service worker; hard refresh of `kasflow-v…` in `sw.js` |

De openbare git-repo is [Ahmadalaina7/kasflow-app](https://github.com/Ahmadalaina7/kasflow-app).
Voor een VPS met Docker in plaats van Plesk: [DEPLOY.md](DEPLOY.md).
