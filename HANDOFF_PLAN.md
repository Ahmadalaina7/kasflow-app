# Kasflow — architectuur en overdracht

Dit document beschrijft hoe Kasflow in elkaar zit en welke keuzes bewust zijn
gemaakt. Het is bedoeld voor wie het project verder bouwt. Praktische instructies
staan in [README.md](README.md) en [DEPLOY.md](DEPLOY.md).

## Wat dit is

Kasflow is boekhoudsoftware voor Nederlandse zzp'ers en kleine bedrijven:
facturen maken, versturen en opvolgen, klanten en bonnetjes beheren, btw per
kwartaal berekenen en je administratie exporteren. Het is geen demo meer: alle
schermen werken op de API, gegevens worden per account gescheiden opgeslagen en
de app kan als PWA op een telefoon of desktop worden geïnstalleerd.

## Opbouw

```
Browser (platte HTML/CSS/JS, geen buildstap)
  app-shell.js        navigatie, mobiele tabbar, API-helper, PWA-installatie, SW-registratie
  app-styles.css      één stylesheet voor alle app-schermen (desktop + mobiel)
  sw.js               service worker: alleen publieke shell cachen, nooit API of ingelogde pagina's
        │  fetch /api/...  (sessiecookie, JSON)
        ▼
FastAPI (backend/app.py)
  config.py           omgevingsvariabelen + productievalidatie (faalt bij onveilige config)
  migrations.py       additieve, idempotente migraties voor SQLite en PostgreSQL
  invoice_pdf.py      factuur-PDF (reportlab) met alle wettelijke velden
  mailer.py           SMTP-verzending met Nederlandse templates en logmodus zonder SMTP
        │  SQLAlchemy
        ▼
SQLite (lokaal)  /  PostgreSQL (productie)  +  uploads-map voor bonnetjes
```

Eén proces, één database, geen queue en geen Redis: dat is genoeg voor honderden
gebruikers en houdt het beheer simpel. De punten waar dat schuurt staan onder
"Grenzen".

## Design-tokens (niet wijzigen zonder reden — dit IS het systeem)

```
--navy-950  #0A1830   diepste achtergrond, sidebar-onder, footer
--navy-900  #0F2244   primaire merkkleur, headers, donkere kaarten
--navy-700  #1D3A66   secundaire vlakken, gradients
--emerald-600 #0E9F6E primaire accent — CTA's, actieve staten
--emerald-400 #34D399 glow-accenten, grafieklijnen, hover
--gold-500  #C9A24B   ALLEEN voor "Meest gekozen" / premium-signaal — spaarzaam
--gray-50   #F6F7F9   paginabasis (lichte secties)
--ink-700 / --ink-500 #33415C / #64748B   body- en hulptekst
--danger    #C2483D   destructieve acties (account verwijderen)
```

Typografie: **Plus Jakarta Sans** (display — koppen, logo, knoppen; 700 voor
koppen, 800 voor de hero, `letter-spacing` tussen -0,02em en -0,035em) en
**Inter** (lopende tekst, bedragen en statistieken, met `tabular-nums` zodat
cijfers in kolommen uitlijnen). Beide worden **zelf gehost** in `fonts/` en
geladen via `fonts.css`; er gaat geen verzoek naar Google Fonts en de CSP
(`font-src 'self'`) staat alleen eigen bestanden toe. De CSS-variabelen heten
`--display`, `--sans` en `--numeric`. Signature element: kaarten met "levende"
cijfers op een donkere navy-gradient met emerald gloed — hergebruik dat motief,
verzin geen nieuw signature-element.

## Datamodel (backend/app.py)

| Tabel | Kern |
| --- | --- |
| `users` | Account, bedrijfsgegevens (adres, IBAN, btw-id), factuurvoorkeuren, e-mailverificatie, sessieversie |
| `customers` | Klant met contactgegevens, adres en btw-nummer voor op de factuur |
| `invoices` | Kop met nummer (uniek **per account**), datums, leverdatum, opmerking, bedragen in centen, `paid_at`, `sent_at` |
| `invoice_lines` | Regel met omschrijving, aantal, stukprijs, btw-tarief in basispunten en berekende bedragen |
| `receipts` | Geüpload bonnetje met metadata; het bestand staat buiten de webmap |
| `vat_declarations` | Interne afvinking van een kwartaal (geen officiële aangifte) |
| `password_resets` | Gehashte, eenmalige en tijdgebonden resettokens |
| `audit_events` | Beveiligingslog per account: inloggen, facturen, export, accountwijzigingen |

Bedragen staan als integer centen; btw-tarieven in basispunten (2100 = 21%).
Alle queries filteren op het ingelogde account.

## API (selectie)

| Route | Doel |
| --- | --- |
| `POST /api/auth/register` / `login` / `logout` | Account, met voorwaarden en optionele verificatie |
| `POST /api/auth/verify-email`, `resend-verification` | E-mailverificatie (verplicht zodra SMTP is ingesteld) |
| `POST /api/auth/forgot-password`, `reset-password` | Wachtwoordherstel met eenmalige link (60 min) |
| `PATCH /api/auth/me` | Bedrijfsgegevens, adres, IBAN, betalingstermijn, voorvoegsel, meldingen |
| `GET /api/account/export`, `GET /api/account/activity`, `DELETE /api/account` | AVG: inzage, activiteit, verwijdering (inclusief bonbestanden) |
| `GET/POST/PATCH/DELETE /api/customers` | Klanten, verwijderen geblokkeerd zolang er facturen zijn |
| `GET /api/invoices`, `GET/POST/PATCH /api/invoices/{id}`, `GET next-number` | Facturen, status, doorlopende nummerreeks |
| `GET /api/invoices/{id}/pdf`, `POST /api/invoices/{id}/send` | PDF volgens factuureisen, verzenden met bijlage |
| `GET/POST/DELETE /api/vat` en `/api/vat/approval` | Btw per kwartaal met interne afvinking |
| `GET/POST/DELETE /api/receipts` | Bonnetjes uploaden, openen en verwijderen |
| `GET /api/health` | Status, database en of SMTP is geconfigureerd |

## Beveiliging (wat waarom)

- **Wachtwoorden**: scrypt met per-gebruiker salt; verificatietokens en
  resettokens worden alleen gehasht opgeslagen en zijn eenmalig en tijdgebonden.
- **Sessies**: ondertekende cookie via `SessionMiddleware`, `HttpOnly`,
  `SameSite=Lax`, `Secure` zodra de publieke URL https is. Een wachtwoordwijziging
  of reset verhoogt `users.session_version`, waarmee alle andere apparaten
  uitloggen.
- **CSRF**: elke mutatie onder `/api/` met een `Origin`-header moet van dezelfde
  host komen. De sessiecookie is `SameSite=Lax`.
- **Koppen**: CSP, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy`; in productie ook HSTS. `TrustedHostMiddleware` op basis
  van `KASFLOW_ALLOWED_HOSTS`.
- **Rate limiting**: in-process sliding window per IP en per account op
  registreren, inloggen, wachtwoordreset, verzenden, exporteren en uploaden.
  Bewust per proces; zie "Grenzen" voor meerdere workers.
- **Bestanden**: bonnen staan in `backend/uploads/<user_id>/`, buiten de webmap;
  de downloadroute controleert eigendom en of het pad binnen de uploadmap valt.
- **Configuratie**: in productie weigert het proces te starten zonder sleutel van
  32+ tekens, met een `http://`-publieke URL of met SQLite.
- **Static assets**: geen contenthash in de bestandsnaam, dus `Cache-Control:
  no-cache` met ETag — na een deploy ziet iedereen direct de nieuwe versie.

## Migraties

`migrations.py` draait bij het starten en is additief en idempotent:

1. Ontbrekende kolommen toevoegen op basis van de SQLAlchemy-metadata
   (werkt op SQLite en PostgreSQL), met een backfill voor kolommen die niet leeg
   mogen zijn. Bestaande accounts krijgen `email_verified_at = created_at`, zodat
   niemand buitengesloten raakt.
2. Eenmalige reparatie: hele oude SQLite-databases hadden een globaal unieke
   factuurnummer; die tabel wordt herbouwd met een unieke sleutel
   `(owner_id, number)`.

Verwijder nooit een kolom in dezelfde uitrol waarin je hem niet meer gebruikt:
eerst uitrollen, later opruimen. Voor een groot team kan dit vervangen worden
door Alembic; de metadata blijft dan de bron van waarheid.

## E-mail

`mailer.py` werkt met elke SMTP-server (Cloud86-mailbox: poort 587 met STARTTLS
of 465 met SSL). Zonder configuratie draait hij in **logmodus**: berichten worden
gelogd en bewaard in `Mailer.outbox`, en e-mailverificatie wordt overgeslagen
zodat lokaal ontwikkelen zonder mailprovider werkt. Zodra SMTP is ingesteld:

- is e-mailverificatie verplicht bij registratie,
- kan een factuur met PDF-bijlage naar de klant,
- gaan verificatie- en resetlinks naar de publieke URL.

## Tests

```bash
python -m pytest backend -q
```

`backend/test_app.py` dekt de basis en de migratie van een oud databaseschema;
`backend/test_product.py` dekt PDF-velden, verzenden met bijlage,
e-mailverificatie, wachtwoordreset, export, verwijderen, nummerreeksen, rate
limiting, voorwaarden en beveiligingskoppen. Beide gebruiken `TestClient` met een
tijdelijke SQLite-database en een opname-mailer in plaats van echte SMTP.

## Grenzen en vervolgstappen

- **Aangifte**: het btw-overzicht bevat alleen btw op eigen facturen; geen
  voorbelasting, geen correcties, geen aangifte bij de Belastingdienst. Direct
  indienen vraagt SBR/Digipoort met een PKIoverheid-certificaat.
- **Bankkoppeling**: bewust nog geen PSD2-koppeling. Kansrijke AIS-providers:
  Enable Banking (per gekoppelde rekening) en Ponto (pay-as-you-go).
- **E-facturatie**: B2B e-invoicing is in Nederland nog niet verplicht (kabinet
  mikt op 1 juli 2030, ViDA op grensoverschrijdende rapportage). Factuurgegevens
  staan klaar om later UBL/Peppol te leveren.
- **OCR**: bonnetjes worden bewaard, niet automatisch uitgelezen.
- **Abonnementen**: geen abonnementslaag of betaalprovider; dat is de logische
  volgende commerciële stap (Mollie of Stripe met iDEAL).
- **Meerdere workers**: zet dan een gedeelde teller (Redis) voor rate limiting en
  overweeg objectopslag voor bonnetjes.
- **Juridische teksten** in `privacy.html`, `voorwaarden.html` en
  `verwerkersovereenkomst.html` zijn concepten: laat ze toetsen en vul
  bedrijfsgegevens, prijzen en opzegtermijnen in voordat je publiceert.
