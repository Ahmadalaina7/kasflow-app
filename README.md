# Kasflow

Kasflow is boekhoudsoftware voor Nederlandse zzp'ers en kleine bedrijven:
facturen maken, versturen en opvolgen, klanten beheren, bonnetjes uploaden, btw
per kwartaal berekenen en je administratie op elk moment exporteren.

De frontend is platte HTML/CSS/JavaScript zonder buildstap en werkt als
installeerbare webapp (PWA) op desktop en mobiel. De backend is FastAPI met
SQLAlchemy, SQLite voor lokaal ontwikkelen en PostgreSQL in productie.

## Snel lokaal starten

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
$env:KASFLOW_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(48))"
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8010
```

Open <http://127.0.0.1:8010>. Maak een account via **Start gratis**; de
marketingsite is publiek, de app zit achter inloggen.

- Database: `backend/kasflow.sqlite3` (wordt automatisch aangemaakt).
- Bonnetjes: `backend/uploads/`.
- Zonder SMTP-instellingen draait e-mail in **logmodus**: mails worden gelogd in
  plaats van verstuurd en e-mailverificatie wordt overgeslagen, zodat je lokaal
  zonder mailprovider kunt werken.

## Tests

```powershell
python -m pytest backend -q
```

De testsuite dekt onder meer: registratie en voorwaarden, e-mailverificatie,
wachtwoordreset, rate limiting, multi-tenant scheiding, btw-berekening,
bonnetjes upload/download/verwijderen, de volledige factuur-PDF met alle
wettelijke velden, factuur verzenden met bijlage, gegevensexport, het definitief
verwijderen van een account en de migratie van een oud databaseschema.

## Wat de app doet

- **Accounts** — registreren met bedrijfsgegevens, inloggen, uitloggen,
  wachtwoord wijzigen en vergeten, e-mailverificatie via een link (48 uur
  geldig), sessie-invalidatie op alle apparaten na een wachtwoordwijziging.
- **Dashboard** — omzet van dit jaar, openstaand en te laat, zesmaandsgrafiek,
  nieuwste facturen en waarschuwingen.
- **Facturen** — aanmaken met meerdere regels en btw per regel (21%, 9% of 0%),
  doorlopende nummerreeks met eigen voorvoegsel, leverdatum en opmerking,
  filteren, zoeken, detailvenster, betaald/onbetaald, CSV-export, **PDF die
  voldoet aan de factuureisen** en **verzenden per e-mail met de PDF als
  bijlage**. Een factuur kan pas worden opgeslagen als alle verplichte gegevens
  er zijn; bedrijfs- en klantgegevens worden bij het aanmaken vastgelegd, zodat
  latere wijzigingen een uitgereikte factuur niet veranderen.
- **Creditfacturen** — een factuur wordt nooit gewijzigd of verwijderd; je
  crediteert hem volledig met één klik en maakt zo nodig een nieuwe.
- **Btw-behandelingen** — normaal, btw verlegd (binnenland), intracommunautaire
  prestatie, uitvoer buiten de EU en de kleineondernemersregeling, elk met de
  verplichte tekst op de factuur.
- **Klanten** — toevoegen, bewerken, verwijderen (geblokkeerd zolang er facturen
  zijn), adres, land en btw-nummer voor op de factuur, openstaand bedrag per klant.
- **Bonnetjes** — uploaden (JPG/PNG/WEBP/PDF) met btw-bedrag, inclusief btw die
  naar jou is verlegd (Google, EU-leveranciers, rubriek 2a/4a/4b).
- **Btw & aangifte** — de kwartaalaangifte omzetbelasting in hele euro's, met
  rubrieken 1a t/m 5c, voorbelasting, verlegde btw op inkopen en de opgaaf ICP
  bij intra-EU omzet. Je neemt de bedragen over in Mijn Belastingdienst Zakelijk.
- **Bank** — CAMT.053 of CSV-afschrift importeren, betalingen koppelen aan
  facturen, privé-uitgaven apart zetten.
- **Uren & kilometers** — registratie voor het urencriterium (1.225 uur) en
  de forfaitaire kilometeraftrek.
- **Jaaroverzicht** — winst-en-verlies, afschrijvingen, investeringsaftrek,
  zelfstandigenaftrek, mkb-winstvrijstelling en een schatting van inkomstenbelasting
  plus Zvw, met wat je maandelijks opzij zet.
- **Instellingen** — bedrijfsgegevens, adres, IBAN, betalingstermijn,
  factuurvoorvoegsel, meldingsvoorkeuren, wachtwoord, recente activiteit,
  volledige gegevensexport en accountverwijdering.
- **Juridisch** — privacyverklaring, algemene voorwaarden en
  verwerkersovereenkomst (conceptteksten, zie de waarschuwing bovenaan elke
  pagina).

## Beveiliging en privacy

- Wachtwoorden als scrypt-hash; vergeet- en verificatielinks worden alleen als
  hash opgeslagen en zijn eenmalig en tijdgebonden.
- Sessiecookie met `HttpOnly`, `SameSite=Lax` en `Secure` zodra de publieke URL
  https is; wachtwoordwijziging verhoogt een sessieversie en logt andere
  apparaten uit.
- Origin-controle op alle mutaties (CSRF), CSP, `nosniff`, `X-Frame-Options`,
  HSTS in productie en een `TrustedHost`-filter op basis van
  `KASFLOW_ALLOWED_HOSTS`.
- Rate limiting op registreren, inloggen, wachtwoordreset, verzenden, exporteren
  en uploaden (per IP en per account).
- Elke query is gekoppeld aan het ingelogde account; bonnen staan buiten de
  webmap en zijn alleen via een gecontroleerde route op te halen.
- Beveiligingslog per account (12 maanden) met inlogpogingen, factuurverzending
  en accountwijzigingen.

## Configuratie

Zie [.env.example](.env.example) voor alle variabelen. De belangrijkste:

| Variabele | Betekenis |
| --- | --- |
| `KASFLOW_SECRET_KEY` | Ondertekent sessies. Verplicht in productie (min. 32 tekens). |
| `KASFLOW_DATABASE_URL` | Standaard SQLite; in productie PostgreSQL. |
| `KASFLOW_PUBLIC_URL` | Publieke https-URL, gebruikt in mails en links. |
| `KASFLOW_SMTP_*` | SMTP-server (bijv. een Cloud86-mailbox) voor systeem- en factuurmails. |
| `KASFLOW_ALLOWED_HOSTS` | Komma-gescheiden hostnamen die de app mogen aanroepen. |
| `KASFLOW_MAX_UPLOAD_MB` | Maximale uploadgrootte voor bonnetjes (standaard 10). |

In productie weigert de app te starten zonder sleutel, met een `http://`-URL of
met SQLite: dat zijn de fouten die anders stil tot datalekken leiden.

## Naar productie

- **Plesk:** [PLESK.md](PLESK.md) — nginx-proxy + gunicorn, PostgreSQL in Plesk,
  Let's Encrypt, back-ups.
- **VPS met Docker:** [DEPLOY.md](DEPLOY.md) — Compose, Caddy, `pg_dump`.

In het kort met Docker:

```bash
cp .env.example .env   # vul sleutels, domein en SMTP in
docker compose up -d --build
```

Cloud86 **shared hosting** kan Kasflow niet draaien (PHP-only, geen Python of
PostgreSQL); gebruik een VPS en de Cloud86-mailbox voor e-mail.

## Bestanden

```
index.html                 marketingsite (eigen <style>)
fonts.css                  @font-face voor de zelf gehoste lettertypen
fonts/                     Plus Jakarta Sans en Inter (.woff2) + OFL-licenties
app-styles.css             gedeeld stylesheet voor alle app-schermen
app-shell.js               navigatie, mobiele tabbar, API-helpers, PWA-installatie
sw.js                      service worker: offline shell, nooit API of ingelogde pagina's cachen
offline.html               offlinepagina
dashboard.html             cijfers, grafiek, openstaande facturen
facturen.html              facturenlijst, filters, detailvenster, PDF en verzenden
factuur.html               nieuwe factuur met regels, leverdatum en opmerking
klanten.html               klanten met adres en openstaand bedrag
bonnetjes.html             bonnetjes uploaden en bekijken
btw.html                   btw per kwartaal met goedkeuring
bank.html                  bankafschrift importeren en koppelen
uren.html                  uren en kilometers
jaaroverzicht.html         winst en schatting inkomstenbelasting
instellingen.html          bedrijfsgegevens, beveiliging, export, verwijderen
login.html / signup.html   inloggen en registreren (met voorwaarden)
verify.html / reset.html   e-mail bevestigen en wachtwoord resetten
privacy.html / voorwaarden.html / verwerkersovereenkomst.html   juridische pagina's
manifest.webmanifest       app-manifest voor installatie op een telefoon
Dockerfile / docker-compose.yml / Caddyfile / DEPLOY.md / PLESK.md   productie
passenger_wsgi.py / plesk/       Plesk (Passenger-fallback en systemd)
backend/app.py             FastAPI-app: modellen, auth, facturen, btw, bonnen, AVG
backend/config.py          omgevingsconfiguratie met productievalidatie
backend/migrations.py      idempotente migraties voor SQLite en PostgreSQL
backend/invoice_pdf.py     factuur-PDF volgens de eisen van de Belastingdienst
backend/mailer.py          SMTP-verzending en Nederlandse mailtemplates
backend/test_app.py        basistests
backend/test_product.py    tests voor PDF, e-mail, verificatie, AVG, beveiliging
HANDOFF_PLAN.md            architectuur en ontwerpkeuzes
```

Nieuwe app-schermen: voeg `app-shell.js` toe, zet `<body data-page="...">` en
hergebruik de `.app-shell` + `.sidebar`-structuur; de shell vult navigatie,
gebruikersblok en mobiele elementen zelf. Neem ook `fonts.css` op, anders valt
de pagina terug op de systeemlettertypen.

## Typografie

Koppen staan in **Plus Jakarta Sans** (700, en 800 voor de hero), tekst en
bedragen in **Inter**. Beide bestanden staan in [fonts/](fonts/) en worden via
[fonts.css](fonts.css) geladen; er gaat geen enkel verzoek naar Google Fonts,
wat sneller is, offline werkt en niet meelekt welke bezoekers de site openen.
De licenties (SIL OFL 1.1) staan ernaast. Heb je een nieuwe stijl nodig, gebruik
dan de bestaande variabele `--display`, `--sans` of `--numeric` in plaats van
een nieuwe lettertypefamilie toe te voegen.

## Grenzen en volgende stappen

- **Aangifte**: het btw-overzicht volgt het factuurstelsel (factuurdatum).
  Privégebruik (1d) en suppleties vul je zelf in als die voorkomen. Kasflow
  dient niet zelf in bij de Belastingdienst; direct indienen vraagt een
  SBR/Digipoort-certificaat.
- **Gedeeltelijk crediteren** kan niet: crediteer volledig en maak een nieuwe
  factuur.
- **Inkomstenbelasting**: het jaaroverzicht is een schatting voor een eenmanszaak
  zonder ander box 1-inkomen, eigen woning of fiscale partner. Kasflow dient
  die aangifte niet zelf in.
- **Bankkoppeling**: er is geen live PSD2-koppeling. Je importeert een afschrift.
- **OCR**: bonnetjes worden bewaard maar niet automatisch uitgelezen.
- **E-facturatie**: B2B e-invoicing is in Nederland nog niet verplicht (het
  kabinet mikt op 1 juli 2030); factuurgegevens zijn er wel op voorbereid.
- **PDF-lettertype**: de gegenereerde factuur-PDF gebruikt de standaard Helvetica
  van de PDF-standaard, niet de huisstijlletters; mee sturen van het merklettertype
  zou de PDF groter maken en vraagt een TTF-variant van het font.
- **Rate limiting** is per proces. Draai je meerdere workers, zet dan een
  gedeelde teller zoals Redis voor de inlog- en verzendroutes.
- **Abo's en betalingen** zijn nog niet gebouwd: er is geen abonnementslaag of
  betaalprovider.
- **Juridische teksten** zijn concepten; laat ze toetsen en vul de
  bedrijfsgegevens in voordat je publiceert.
