# Kasflow op deze Plesk-host (LiteSpeed)

Deze hosting serveert bestanden met **LiteSpeed** en heeft **PHP**, maar geen
systemd, geen Passenger en geen langlopend Python-proces. Daarom werkt Kasflow
hier zo:

```
browser ──► LiteSpeed ──► .htaccess ──► proxy.php ──► python plesk/cgi_app.py ──► FastAPI
             (HTML/CSS)                                   (per verzoek, via a2wsgi)
```

De pagina's worden gewoon als bestanden geserveerd; alleen `/api/...` gaat naar
Python. Er is geen poort 8010, geen cron-taak en geen service die kan stoppen.

## Na elke deploy: twee dingen

1. **Plesk → Git → Deploy** (of `git pull` in de documentroot). Zonder dat blijft
   de oude code staan.
2. Open `https://kasflow-app.webnestiq.nl/api/health` één keer.

Het eerste verzoek kan **1–2 minuten** duren: dan maakt `plesk/bootstrap.sh`
de venv en installeert het de packages. Daarna is het antwoord:

```json
{"status":"ok","version":"2.0.0","database":"ok","mail":"log","env":"production"}
```

Alleen `"status":"ok"` betekent dat de app echt draait. Duurt het langer of komt
er een fout, dan zegt de melding nu wát er mis is (zie hieronder).

## Hoe de onderdelen werken

| Bestand | Rol |
| --- | --- |
| `.htaccess` | stuurt `/api/...` naar `proxy.php`; blokkeert `backend/`, `plesk/`, `.env` en `.git` |
| `proxy.php` | zorgt dat venv en packages klaar zijn en start daarna Python |
| `plesk/cgi_app.py` | vertaalt één CGI-verzoek naar de FastAPI-app |
| `plesk/bootstrap.sh` | maakt de venv, installeert `backend/requirements.txt`, maakt `.env` |
| `backend/logs/cgi.log` | de volledige foutmelding als de app niet start |

`proxy.php` onthoudt met `backend/logs/deps.ok` (een md5 van
`backend/requirements.txt`) of de packages al kloppen. Verandert dat bestand,
dan installeert het de volgende keer automatisch opnieuw.

### Configuratie

De eerste keer maakt `bootstrap.sh` een `.env` met SQLite en een eigen
geheime sleutel. Wil je PostgreSQL uit **Plesk → Databases**, pas dan
`.env` aan en verwijder `KASFLOW_ALLOW_SQLITE`:

```env
KASFLOW_DATABASE_URL=postgresql+psycopg://GEBRUIKER:WACHTWOORD@localhost:5432/DATABASENAAM
```

Wachtwoorden met `@ : / # %` moeten URL-encoded (`@` wordt `%40`). Zet ook
`KASFLOW_PUBLIC_URL` en `KASFLOW_ALLOWED_HOSTS` op je eigen domein. SMTP en
Mollie zijn optioneel; zonder die waarden start de app gewoon (mail gaat dan
naar het log en betalen staat uit). Zie [DEPLOY.md](DEPLOY.md) §4 en §5 voor de
verdere invulling.

## Grenzen van deze opzet

- **Snelheid**: per API-verzoek start Python opnieuw (ongeveer 0,3–1 s). Voor
  een enkel account is dat werkbaar; bij meerdere klanten is een VPS met Docker
  ([DEPLOY.md](DEPLOY.md)) de juiste stap.
- **SQLite**: één bestand, `backend/kasflow.sqlite3`. Back-up = dat bestand plus
  `backend/uploads/`. Verplaats die twee nooit buiten de projectmap zonder
  `KASFLOW_DATABASE_URL` en `KASFLOW_UPLOAD_DIR` aan te passen.
- **Registreren** is beperkt tot 5 per uur per IP-adres.

## Als `/api/health` niet `"status":"ok"` geeft

| Melding | Oorzaak en oplossing |
| --- | --- |
| `Python kon niet worden klaargezet …` | De venv of `pip install` lukte niet. De melding bevat de laatste regel uit `bootstrap.sh`; vaak blokkeert de host PyPI of ontbreekt Python 3. |
| `De Python-packages ontbreken nog …` | `bootstrap.sh` stopte halverwege. Draai hem met de hand: `bash plesk/bootstrap.sh`. |
| `Kasflow kon niet starten (…)` | De app zelf faalt bij het starten; de melding noemt de fout. Volledige traceback: `backend/logs/cgi.log`. |
| `De app stopte zonder antwoord …` | Meestal een time-out van 120 s of een ontbrekende Python. |
| `Invalid production configuration` | `.env` mist een sleutel van 32+ tekens, `https://` in `KASFLOW_PUBLIC_URL`, of PostgreSQL. |
| `403 TrustedHost …` | `KASFLOW_ALLOWED_HOSTS` komt niet overeen met het domein. |
| `500` met `A server error occurred` | Fout in de app tijdens het antwoorden; zie `backend/logs/cgi.log`. |
| Pagina's laden, `/api` geeft een HTML-404 | `.htaccess` staat niet in de documentroot, of de documentroot is niet de projectmap. |
| Styling weg of oude pagina | Service worker: hard refresh (Ctrl+Shift+R) of de versie in `sw.js` ophogen. |

Draai je ooit vanaf SSH op de server, dan is dit de snelste controle:

```bash
cd /pad/naar/documentroot
bash plesk/bootstrap.sh          # eenmalig of na een wijziging in requirements
curl -sS http://127.0.0.1/api/health   # alleen als er een proxy naar PHP loopt
php -r 'echo "php ok\n";'
```

## Wat hier bewust niet staat

Passenger, systemd-services, nginx-proxyblokken en een gunicorn op poort 8010
werken op deze host niet (LiteSpeed, geen root). Die routes staan niet meer in
deze map; voor een server waar je wél root hebt is er [DEPLOY.md](DEPLOY.md)
met Docker, PostgreSQL en Caddy.

De openbare repo is [Ahmadalaina7/kasflow-app](https://github.com/Ahmadalaina7/kasflow-app).
