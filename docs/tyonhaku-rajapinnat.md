# Suomen työnhakuportaalit ja rajapinnat — tutkimuskooste

**Päivitetty:** 20.6.2026 (uudelleentarkistettu ja integroitu samana päivänä)  
**Tutkimus:** verkkodokumentaatio, viralliset ohjeet, OpenAPI-spesifikaatiot, RSS/sitemap-kartoitus, scrape vs. API -testaus ja suora curl/Python-verifiointi

---

## Dokumentin rakenne

Tämä on työnhakuportaaleja ja rajapintoja koskeva yksi totuus. Aiemmat erilliset master-, pikakooste-, scrape-tutkimus- ja luonnosdokumentit on yhdistetty tähän tiedostoon, jotta samasta aiheesta ei ole rinnakkaisia versioita.

**Lukujärjestys:** 1–8 lähteet ja vertailu → 9 suositukset → 10 testaus → 11 RSS/sitemap → **12 ohjelmallinen keruu, curl-esimerkit ja scrape-huomiot** → 13 linkit → 14 vastuullinen käyttö.

**Tämän projektin tavoite:** paikallinen Docker Compose -työnhakuportaali yhdelle työnhakijalle — automaattinen keruu, deduplikointi, haku ja suositukset ilman yksityisiä API-sopimuksia. Ajastettu keruu käyttää oletuksena kaikkia verifioituja ei-estettyjä harvestereita (`COLLECTOR_ENABLED_SOURCES=duunitori,tmt,tmt_oulu,laura,jobly,eures_fi,kuntarekry,kirkkorekry,oulu_varbi`). Duunitori, TMT ja Laura ovat edelleen laajimmat MVP-tason lähteet; muut lähteet täydentävät kattavuutta. Tuotetavoite ja laatukriteerit: [`goal.md`](goal.md). Ajantasainen toteutus: [`implementation-journal.md`](implementation-journal.md).

**Toistettavat testit:** [`../scrape-test/`](../scrape-test/).

---

## Tiivistelmä

Suomessa **yksittäisten työpaikkailmoitusten ajantasainen haku** on mahdollista usealla eri tasolla:

| Lähde | Julkinen ilman sopimusta? | Yksittäiset ilmoitukset? | Ajantasaisuus | Testattu 20.6.2026 |
|---|---|---|---|---|
| **TMT verkkosivun haku-API** (epävirallinen) | Kyllä | Kyllä | Reaaliaikainen | ✅ 11 327 ilmoitusta |
| **EURES (EU)** | Kyllä | Kyllä | Reaaliaikainen | ✅ 12 005 suom. ilmoitusta |
| **Työmarkkinatori KIPA P67** (virallinen) | Ei — KEHA-sopimus | Kyllä | Reaaliaikainen | ⚠️ Vaatii IP-avaukset |
| **Careerjet** | Ei — API-avain | Kyllä (aggregaatti) | Reaaliaikainen | ⚠️ HTTP 401 ilman avainta |
| **Tilastokeskus StatFin PxWeb** | Kyllä | Ei — aggregaatit | Kuukausittain | ✅ 34 865 (2026M04) |
| **Duunitori jobentries API** (epävirallinen) | Kyllä | Kyllä | Reaaliaikainen | ✅ 16 809 ilmoitusta |
| **Laura.fi WP REST** (epävirallinen) | Kyllä | Kyllä | Reaaliaikainen | ✅ 11 997 ilmoitusta |
| **Jobly sitemap** | Kyllä (ei API:a) | Kyllä (URL + JSON-LD) | `lastmod` tunteja | ⚠️ ~13 779 URL |
| **Indeed.fi** | Ei | — | — | ❌ HTTP 403 |
| **RSS-syötteet (työpaikat)** | Ei yleistä julkista feediä | Rajallinen | Vaihtelee | ⚠️ Luku 11 |

**Keskeinen johtopäätös:** Virallinen, sopimusperusteinen reitti on **Työmarkkinatorin KIPA-rajapintojen** kautta. Käytännössä **TMT:n verkkosivun sisäinen haku-API**, **Duunitorin jobentries-API**, **Laura REST** ja **EURES** ovat nopeimmin käyttöönotettavat vaihtoehdot — ne toimivat ilman rekisteröitymistä ja palauttavat ajantasaista dataa.

Ilmoitusmäärät taulukoissa ovat **snapshot 20.6.2026** — live-luvut vaihtelevat. Ajantasainen tarkistus: `make test-regression`. Koneellinen läherekisteri: [`sources.yaml`](sources.yaml).

Toimivien endpointtien copy-paste-esimerkit ja scrape-vs-API-suositukset ovat luvussa 12.

---

## 1. Työmarkkinatori (KEHA-keskuksen palvelu)

Työmarkkinatori on Suomen virallinen työnvälitysalusta (korvasi mol.fi:n). Se kattaa julkiseen työnvälitykseen ilmoitetut paikat ja suuren osan kaupallisten portaalien ulkopuolisista ilmoituksista.

---

### 1.1 Verkkosivun sisäinen haku-API (dokumentoimaton, avoin)

Työmarkkinatorin hakuwidget käyttää autentikoimatonta REST-API:a samalla domainilla. Tätä ei ole erikseen dokumentoitu kumppaneille, mutta se on teknisesti avoin ja palauttaa ajantasaista dataa.

#### Haku

```http
POST https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search
Content-Type: application/json

{
  "query": "ohjelmistokehittäjä",
  "filters": {},
  "paging": { "pageNumber": 0, "pageSize": 20 }
}
```

**Huomio:** `filters.publishedAfter` toimii incrementaalihakuun. Testattu raja on sama LATEST- ja `publishedAfter`-moodeissa: `pageSize=90` toimii, `pageSize>=99` palauttaa HTTP 400. Curl-esimerkeissä käytetään pienempiä arvoja luettavuuden vuoksi. Älä käytä `PUBLICATION_TIME_DESC` (→ 400).

**Uusimmat ilmoitukset:**

```json
{
  "query": "",
  "filters": {},
  "paging": { "pageNumber": 0, "pageSize": 20 },
  "sorting": "LATEST"
}
```

**Incrementaali (julkaistu tietyn ajankohdan jälkeen):**

```json
{
  "query": "",
  "filters": { "publishedAfter": "2026-06-19T00:00:00.000Z" },
  "paging": { "pageNumber": 0, "pageSize": 20 }
}
```

**Vastaus (JSON):** `totalElements`, `pageSize`, `lastPage`, `content[]` (id, title, employer, applicationPeriodEndDate, created, …)

#### Yksittäinen ilmoitus

```http
GET https://tyomarkkinatori.fi/api/jobposting-new/v1/public/jobpostings/{uuid}
```

Palauttaa täyden ilmoituksen: kuvaukset, ESCO-ammatit, sijainti, palkkaus, yhteystiedot, metadata (created, etag, status).

#### Koodistot (avoin)

```http
GET https://tyomarkkinatori.fi/api/codes/v1/koodistot
```

Palauttaa luettelon koodistoista (KIELI, KUNTA, ESCO_AMMATTI jne.).

#### Testitulokset (20.6.2026)

| Testi | Tulos |
|---|---|
| Tyhjä haku, kaikki avoimet | **11 327** ilmoitusta (`totalElements`) |
| Haku "ohjelmistokehittäjä" | **35** osumaa |
| Suodatin kunta 091 (Helsinki) | **1 822** ilmoitusta |
| Julkaistu eilisen jälkeen (`publishedAfter`, `pageSize=90` toimii) | **33** ilmoitusta |
| Yksittäisen ilmoituksen GET | ✅ Täysi JSON-vastaus |
| Koodistot GET | ✅ 211 koodistoa |
| Hakuvahti (watch v2) | ✅ Endpoint vastaa (HTTP 400 validoinnilla) |
| Haku v1 | ❌ HTTP 404 (poistunut) |

**Hakuvahti (sähköposti, ei RSS):**

```http
POST https://tyomarkkinatori.fi/api/jobpostingfulltext/watch/v2/register
Content-Type: application/json

{
  "email": "user@example.com",
  "query": "sairaanhoitaja",
  "filters": {},
  "interval": 7,
  "language": "fi"
}
```

Endpoint on olemassa ja validoi syötteen; se rekisteröi sähköpostihälytyksen, ei RSS-syötettä.

**Huomio:** `sorting`-parametri on herkkä — virheellinen arvo palauttaa HTTP 400. Jätä pois tai käytä oletusta. Tämä API on sivuston sisäinen toteutus; virallinen reitti integraatioihin on KIPA P67. Tuotantokäyttöä varten kannattaa varmistaa oikeudellinen asema (käyttöehdot, kuormitus, lähdemerkintä).

---

### 1.2 Viralliset KIPA-rajapinnat (B2B, sopimus)

Dokumentaatio: [Työpaikkailmoitusten rajapinnat](https://tyomarkkinatori.fi/ohjeet-ja-tuki/rajapinnat/tyopaikkailmoitusten-rajapinnat)

| Rajapinta | Entinen nimi | Tarkoitus | Versio | Tuotantopäätepiste |
|---|---|---|---|---|
| **P67 Hakurajapinta** | Noutorajapinta | Ilmoitusten haku ulkoisiin palveluihin | 2.0 | `https://api.ahtp.fi/kipa/p67/v2/jobpostings` |
| **P66 Hallintarajapinta** | Tuontirajapinta | Ilmoitusten luonti, päivitys, poisto | 1.0 | `https://api.ahtp.fi/kipa/p66/v1/jobposting/{businessId}/{externalId}` |

**Testiympäristö:** `https://api-qa.ahtp.fi/kipa/p66|p67/...`

#### P67 hakurajapinta — tekniset tiedot (v2)

- **Protokolla:** REST, **HTTP POST** (v2:ssa metodi vaihtui GETistä POST:iin, jotta suodattimet saadaan helpommin mukaan kutsuun)
- **Autentikointi:** Bearer JWT + `KIPA-Subscription-Key` -header
- **Vastaus:** NDJSON-stream (newline-delimited JSON, yksi ilmoitus per rivi)
- **OpenAPI YAML:** [https://tyomarkkinatori.fi/jobpostingProvider/swagger/api-docs/P67-tmt-provider-haku-V2](https://tyomarkkinatori.fi/jobpostingProvider/swagger/api-docs/P67-tmt-provider-haku-V2) ← julkisesti saatavilla, HTTP 200 ✅
- **Tekninen dokumentaatio:** [https://tyomarkkinatori.fi/jobpostingprovider/documentation/KIPA-search-jobpostings-fi.html](https://tyomarkkinatori.fi/jobpostingprovider/documentation/KIPA-search-jobpostings-fi.html)

**Suodattimet (kaikki valinnaisia):**

```json
{
  "onlyStatus": "PUBLISHED",
  "published":  { "from": "2026-06-01T00:00:00Z", "to": null },
  "modified":   { "from": "...", "to": "..." },
  "created":    { "from": "...", "to": "..." },
  "expires":    { "from": "...", "to": "..." },
  "employerTypeIn":      ["YRITYS"],
  "workTimeIn":          ["FT"],
  "continuityOfWorkIn":  ["VK"],
  "postingLanguageIn":   ["fi", "sv", "en"],
  "countryIn":           ["FI"],
  "regionIn":            ["01"],
  "municipalityIn":      ["091"],
  "occupationIscoIn":    ["2211.1"]
}
```

**Palautuva tietosisältö per ilmoitus (OpenAPI-skeemasta):**

- `metadata` — luontipäivä, muokkauspäivä, julkaisupäivä, arkistointipäivä, externalId
- `owner` — työnantajan y-tunnus, nimi, toimialakoodi, toimipaikkatiedot, tyyppi
- `client` — toimeksiantajan tiedot (esim. rekrytointiyritys)
- `position` — ammatti (ESCO/ISCO), osaamiset, palvelussuhde, palkka ja palkkahaitari, työaika, työn jatkuvuus, ajokortit, luvat, työn kuvaus, otsikko (monikielinen)
- `location` — maa, maakunta, kunta, katuosoite, postinumero, matkustamisvaatimus
- `application` — hakuaika (julkaisu- ja päättymispäivä), hakemuksen URL, avointen paikkojen lukumäärä
- `contacts` — yhteyshenkilöt (nimi, email, puhelin)
- `externalLinks` — ulkoiset linkit

**Esimerkki API-kutsusta (kun tunnukset saatu):**

```bash
curl -X POST "https://api.ahtp.fi/kipa/p67/v2/jobpostings" \
  -H "Authorization: Bearer <JWT>" \
  -H "KIPA-Subscription-Key: <AVAIN>" \
  -H "Content-Type: application/json" \
  -d '{"onlyStatus":"PUBLISHED","countryIn":["FI"]}'
```

#### P66 hallintarajapinta — tekniset tiedot

- **Metodit:** GET / PUT / DELETE
- **Autentikointi:** `KIPA-Subscription-Key`
- **Käyttötapaukset:** rekrytointi- ja ATS-järjestelmät (Teamtailor, SAP SuccessFactors jne.)
- **Luokitus:** ESCO + FINESCO-kansallinen laajennus (EURES-yhteensopiva)

#### Käyttöönotto

1. Täytä [käyttöönottoilmoituslomake](https://link.webropolsurveys.com/Participation/Public/675df7cd-3222-48a0-bc8d-f089b95315a5?displayId=Fin3601513)
2. Hyväksy [käyttöehdot](https://tyomarkkinatori.fi/ohjeet-ja-tuki/rajapinnat/tyopaikkailmoitusten-rajapinnat/tyomarkkinatorin-tyopaikkailmoitusten-rajapintojen-kayttoehdot)
3. KEHA-keskus tarkistaa organisaation (Y-tunnus) ja avaa **IP-osoitteet + API-avaimet**
4. Testaus testiympäristössä → ilmoitus KEHA:lle → tuotantotunnukset

Yhteystiedot: `tmt-rajapinnat@keha-keskus.fi`

#### Siirtymäaikataulu

| Tapahtuma | Päivämäärä |
|---|---|
| Vanha mol.fi-integraatio poistunut | 31.5.2024 |
| Uudistettu työpaikkailmoitus julkaistu | 24.3.2026 |
| Uudet rajapinnat (P67 v2) käytössä | Huhtikuusta 2026 |
| Kaikki uudet avaukset vain v2:lla | Huhtikuusta 2026 |
| Vanhat rajapinnat poistuvat | **Toukokuu 2027** |

#### Käyttöehdoista (hakurajapinta)

- Lähde mainittava: *"Lähde: Työmarkkinatorin asiakastietojärjestelmä"*
- Dataa **ei saa luovuttaa eteenpäin** kolmansille ilman lupaa
- Poistuneet ilmoitukset on poistettava omasta palvelusta viipymättä
- Kaikki TMT-ilmoitukset synkronoituvat automaattisesti **EURES-portaaliin**

#### Testitulos (20.6.2026, uudelleentarkistettu)

| Endpoint | Tulos |
|---|---|
| `https://api.ahtp.fi/kipa/p67/v2/jobpostings` | **Timeout** (8 s) |
| `https://api-qa.ahtp.fi/kipa/p67/v2/jobpostings` | **Timeout** (8 s) |
| `https://api.ahtp.fi/kipa/p66/v1/jobposting/...` | **Timeout** (8 s) |
| `https://integraatiot.tyomarkkinatori.fi/jobpostingprovider/v1/tyopaikat` | **Timeout** (10 s) |

Rajapinnat ovat suojattuja: ilman KEHA-keskuksen tunnuksia ja IP-sallintaa ne eivät vastaa julkisesta internetistä. Vanha `integraatiot.tyomarkkinatori.fi`-domain on v1.0-dokumentaatiossa, mutta se ei ole käytettävissä ilman sopimusta.

---

### 1.3 Muut Työmarkkinatorin rajapinnat

| Rajapinta | Suunta | Tila | Huomio |
|---|---|---|---|
| Työnhakuprofiilin tuonti | Ulko → TMT | Käytössä | Vaatii käyttäjän suostumuksen |
| Työnhakuprofiilin nouto | TMT → ulko | **Syksy 2026** | Ei vielä avoinna |
| Työvoimakoulutusten hakurajapinta | Haku | Käytössä | Koulutukset ja valmennukset, ei työpaikkoja. Yhteys: `tmt-koulutustietorajapinta@keha-keskus.fi` |

---

## 2. EURES (EU:n työnvälitysverkosto)

EURES aggregoi Työmarkkinatorin ilmoituksia (lähde `TMT`) sekä muiden EU-maiden dataa. Suomalaiset ilmoitukset tulevat EURES:iin automaattisesti Työmarkkinatorin kautta.

**Base URL:** `https://europa.eu/eures/api`

**Ilmoituksen selain-URL:** `https://europa.eu/eures/portal/jv-se/jv-details/{url-encoded-id}?jvDisplayLanguage=fi&lang=fi`

### Testatut päätepisteet

| Metodi | Polku | Kuvaus |
|---|---|---|
| POST | `/jv-searchengine/public/jv-search/search` | Työpaikkahaku |
| GET | `/jv-searchengine/public/jv/id/{base64-id}` | Yksittäinen ilmoitus |
| GET | `/jv-searchengine/public/statistics/getNumberOfJobs` | Kaikki työpaikat (globaali) |
| GET | `/jv-searchengine/public/statistics/getCountryStats` | Maakohtaiset luvut |

### Esimerkkihaku (Suomi)

```bash
curl -X POST 'https://europa.eu/eures/api/jv-searchengine/public/jv-search/search' \
  -H 'Content-Type: application/json' \
  -d '{
    "resultsPerPage": 5,
    "page": 1,
    "sortSearch": "MOST_RECENT",
    "keywords": [{"keyword": "ohjelmistokehittäjä", "specificSearchCode": "EVERYWHERE"}],
    "locationCodes": ["fi"],
    "occupationUris": [],
    "skillUris": [],
    "requiredExperienceCodes": [],
    "positionScheduleCodes": [],
    "sectorCodes": [],
    "educationAndQualificationLevelCodes": [],
    "positionOfferingCodes": [],
    "euresFlagCodes": [],
    "otherBenefitsCodes": [],
    "requiredLanguages": [],
    "sessionId": "unique-session-id",
    "requestLanguage": "fi"
  }'
```

### Testitulokset (20.6.2026)

| Mittari | Arvo |
|---|---|
| Suomen työpaikat (`numberRecords`) | **12 005** (uudelleentarkistus: ~11 999) |
| Suomen avoimet paikat (posts) | **23 144** |
| Haku "ohjelmistokehittäjä" + fi | **34** osumaa |
| Yksittäisen ilmoituksen `source` | **`TMT`** (Työmarkkinatori) |
| Globaalit työpaikat | **2 981 292** |
| Vastauskentät | `numberRecords`, `jvs[]` — **ei** `numberOfRecords` / `jobVacancies` |
| Minimal payload (vain `locationCodes`, `keywords`) | ✅ Toimii |
| `sortSearch: MOST_RECENT` | ⚠️ Epäluotettava järjestys incrementaalipollaukseen |

EURES näyttää ~678 enemmän ilmoitusta kuin TMT:n oma haku — ero johtuu todennäköisesti indeksointiviiveestä, duplikaateista tai eri tilamääritelmistä.

**Plussat:** Ei rekisteröitymistä, monikielinen, ESCO-pohjainen, hyvä kansainväliseen hakuun.  
**Miinukset:** Ei virallista julkaistua OpenAPI-spesifikaatiota (reverse-engineered dokumentaatio: [EURES-API-Documentation](https://github.com/rorar/EURES-API-Documentation)); HTML-kuvaukset; EU-palvelun käyttöehdot omat.

---

## 3. Tilastokeskus — Työnvälitystilasto (PxWeb API)

**Ei yksittäisiä ilmoituksia** — aggregoituja virallisia lukuja KEHA-keskuksen tuottamasta työnvälitystilastosta.

- Dokumentaatio: [Avoin data ja rajapinnat](https://stat.fi/fi/palvelut/tilastodatapalvelut/avoin-data-ja-rajapinnat)
- API: `https://pxdata.stat.fi/PxWeb/api/v1/fi/StatFin/tyonv/{taulukko}.px`
- Lisenssi: CC BY 4.0

### Esimerkki: avoimet työpaikat koko maassa

```bash
curl -X POST 'https://pxdata.stat.fi/PxWeb/api/v1/fi/StatFin/tyonv/12r5.px' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": [
      {"code": "Alue", "selection": {"filter": "item", "values": ["SSS"]}},
      {"code": "contentscode", "selection": {"filter": "item", "values": ["AVPAIKATLOPUSSA", "UUDETAVP"]}},
      {"code": "timeperiod_m", "selection": {"filter": "item", "values": ["2026M04"]}}
    ],
    "response": {"format": "json-stat2"}
  }'
```

**Tulos huhtikuu 2026 (koko maa):**

- Avoimet työpaikat kuukauden laskentapäivänä: **34 865**
- Uudet avoimet työpaikat kuukauden aikana: **37 012**

**Ajantasaisuus:** kuukausittainen (viimeisin data 2026M04). Ero TMT:n 11 327 avoimeen ilmoitukseen johtuu eri määritelmistä: tilasto sisältää kaikki TE-toimistoille ilmoitetut paikat, TMT-haku näyttää aktiivisesti portaalissa julkaistuja ilmoituksia.

---

## 4. Tutkimusaineistot (ei reaaliaikainen haku)

| Aineisto | Lähde | Sisältö | Ajantasaisuus |
|---|---|---|---|
| Julkiseen työnvälitykseen ilmoitetut avoimet paikat | [FSD3971 / Aila](https://services.fsd.tuni.fi/catalogue/FSD3971) | Vuosittainen rekisteri tutkimuskäyttöön | Vuosittain (2024 saatavilla) |
| Vakanssidata | Työ- ja elinkeinoministeriö → FSD | Täyttöprosessi, kesto, ammatti, sijainti | Historiallinen |

Soveltuu analyysiin, ei työnhakusovelluksen reaaliaikaiseen syötteeseen.

---

## 5. Kaupalliset työnhakuportaalit

### 5.1 Duunitori.fi

| Ominaisuus | Tila |
|---|---|
| Virallinen dokumentoitu haku-API | **Ei** |
| Epävirallinen haku-API (`/api/v1/jobentries`) | **Kyllä** — testattu 20.6.2026 |
| Testatut polut `/api/jobs` | HTTP 404 |
| `api.duunitori.fi` | DNS ei resolvdu |

Duunitori on Suomen suurin kaupallinen työpaikkaportaali. Duunitorin UKK:n mukaan se **kerää ilmoituksia automaattisesti julkisista lähteistä, kuten Työmarkkinatorilta** — eli osa Duunitorin sisällöstä on peräisin TMT:stä, mutta Duunitorilla on myös **omia ilmoituksia**, joita TMT:ssä ei näy.

#### Epävirallinen haku-API (dokumentoimaton, avoin)

```http
GET https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=100
User-Agent: Mozilla/5.0
```

**Vastaus (JSON):** `count`, `next`, `results[]` — kentät `heading`, `date_posted`, `slug`, `company_name`, `municipality_name`, **`descr`** (koko ilmoituksen teksti listassa).

**Testitulokset 20.6.2026:** **16 809** ilmoitusta; uusimmat `ordering=-date_posted`; max `page_size=100`. Jos pyydät yli 100 (`page_size=200`), API palauttaa virheettä vain 100 tulosta. Yksittäisen ilmoituksen `GET /api/v1/jobentries/{slug}` → **404** (ei tuettu).

| Ominaisuus | Tila | Testattu |
|---|---|---|
| **Haku-API jobentries** | **Kyllä** (epävirallinen) | `GET /api/v1/jobentries` → 200, JSON |
| Julkinen haku-API `/api/jobs` | **Ei** | `/api/jobs` → 404 |
| RSS/Atom työpaikoille | **Ei** | `/rss`, `/feed`, `/tyopaikat/rss` → 404 |
| OpenSearch | **Kyllä** | `https://duunitori.fi/opensearch.xml` → HTML-hakupohja |
| Sitemap (työpaikat) | **Kyllä** | `https://duunitori.fi/sitemap-jobentry.xml` → URL-lista + `lastmod` |
| Duunivahti | Sähköposti | Ei RSS |

**OpenSearch** mahdollistaa selaimen hakukoneintegraation, ei strukturoidun datan noutoa:

```xml
<!-- https://duunitori.fi/opensearch.xml -->
<Url type="text/html" template="https://duunitori.fi/tyopaikat/?haku={searchTerms}"/>
```

**Sitemap** on hyvä **varajae** URL-luetteloon ja `lastmod`-seurantaan; ensisijainen tapa on **jobentries-API**:

```xml
<!-- https://duunitori.fi/sitemap-jobentry.xml (paginoitu ?p=2, ?p=3, ...) -->
<url>
  <loc>https://duunitori.fi/tyopaikat/tyo/pizza-crew-sdsuu-20380957</loc>
  <lastmod>2026-06-20</lastmod>
</url>
```

Työnantajille suunnattu XML/RSS/JSON-syöte on **push-suuntaan** (ilmoitusten vienti Duunitoriin), ei virallista dokumentoitua pull-API:a. Ohjelmallinen haku: **jobentries-API** (suositus) tai sitemap + HTML/JSON-LD. Katso luku 12.

### 5.2 Jobly.fi (ent. Monster.fi, Alma Media)

| Ominaisuus | Tila |
|---|---|
| Julkinen hakurajapinta | **Ei** |
| Julkaisurajapinta (ATS → Jobly) | **Kyllä** — vain kumppaneille |

Jobly.fi on Alma Median työnhakuportaali. Mainostaa tavoittavansa Iltalehden, Kauppalehden ja Talouselämän yleisön.

**Julkaisu-API (vain ilmoittajille, ei hakuun):**

```http
POST https://www.jobly.fi/api/v1/job?key={APIKEY}&action=add
Content-Type: application/xml
```

- DELETE: `?action=delete&id={job_id}`
- Dokumentaatio: [jobly.almamedia.fi/api](https://jobly.almamedia.fi/api/)
- **Ei sandboxia** — testi-ilmoitukset menevät liveen
- Avain: `tech@jobly.fi` / Alma Career integrations

| Syöte | URL | Tulos |
|---|---|---|
| RSS / Atom | `/rss`, `/feed` | ❌ HTTP 404 |
| `/rss.xml` | `https://www.jobly.fi/rss.xml` | ⚠️ HTTP 200 mutta palauttaa HTML:n, ei RSS:ää |
| Hakusivut | `/tyopaikkailmoitus/hae`, `/tyopaikka` | ❌ HTTP 404 (20.6.2026) — ei staattista listaa |
| **Sitemap** | `https://www.jobly.fi/sitemap.xml?page=1` (+ `page=2`) | ✅ **9 569 + 4 210 = 13 779** työ-URL |
| Yksityissivu JSON-LD | `/tyopaikka/{slug}-{id}` | ⚠️ JobPosting osassa ilmoituksista |

**Ohjelmallinen keruu (ei julkista hakurajapintaa):**

1. Pollaa sitemap `lastmod` → uudet/päivitetyt URL:t (77 kpl päivitetty 20.6.2026: sivu 1 = 31, sivu 2 = 46)
2. Hae yksityissivu → `application/ld+json` JobPosting tai HTML
3. Headless-selain vain jos JSON-LD puuttuu

**Selaintestihavainto:** Playwright löysi `/tyopaikat`-sivulta 22 ilmoituselementtiä per sivu, mutta XHR-kuuntelu ei paljastanut erillistä JSON-hakurajapintaa. Kahdeksan sivua kattaisi vain noin 176 ilmoitusta, joten sitemap on edelleen ensisijainen lähde.

Katso luku 12.3.

### 5.3 Oikotie Työpaikat

**Palvelu suljettu 28.2.2025.** Oikotie ei enää tarjoa työnhakupalvelua. Oikotie tarjoaa edelleen API:a **asuntopalveluille** ([Listing API](https://docs.asunnot.oikotie.fi/listing-api/)) — REST, vaatii sopimuksen, vendor ID:n ja API-avaimen.

### 5.4 Laura.fi (ent. Rekrytointi.com)

| Ominaisuus | Tila |
|---|---|
| Virallinen dokumentoitu API | **Ei** |
| WordPress REST (`/wp-json/wp/v2/job-listings`) | **Kyllä** — testattu 20.6.2026 |
| Testatut `/api/jobs`, `/api/v1/jobs` | HTTP 301 (HTML-sivu) |

Laura käyttää **WP Job Manager** -pluginia. Listaus latautuu selaimessa AJAX:lla, mutta **REST on avoin** ilman autentikointia:

```http
GET https://laura.fi/wp-json/wp/v2/job-listings?per_page=100&orderby=date&order=desc
```

**Vastausheaderit:** `X-WP-Total` (11 997), `X-WP-TotalPages` (120 kun `per_page=100`). Yksittäinen: `GET .../job-listings/{id}` → täysi `content.rendered`.

Laura julkaisee suomalaisten yritysten avoimia paikkoja; data **limittyy läheisesti TMT:hen** ylimmässä listauksessa, mutta Laura sisältää myös muita lähteitä (yhteensä enemmän ilmoituksia kuin pelkkä TMT-haku).

| Syöte | URL | Sisältö | Testattu |
|---|---|---|---|
| **WordPress REST job-listings** | `/wp-json/wp/v2/job-listings` | JSON, täysi kuvaus | ✅ 11 997 ilmoitusta |
| WordPress RSS (sivusto) | `https://laura.fi/feed/` | Tyhjä kanava (0 item) | ✅ HTTP 200, ei työpaikkoja |
| WordPress RSS (avoimet työpaikat) | `https://laura.fi/avoimet-tyopaikat/feed/` | Kommenttisyöte, 0 item | ✅ HTTP 200, ei työpaikkoja |
| Rekrytointijärjestelmän RSS | Asiakaskohtainen | Työnantajan urasivulle | Vain Laura-asiakkaille |
| Hakuvahti | Web UI | Sähköposti | Ei RSS |

Laura Rekrytointijärjestelmä tarjoaa **Extended Job Feed** -RSS:n työnantajille (oma urasivu/intranet), mutta URL on asiakaskohtainen — ei yhtä julkista feediä kaikille Laura.fi-ilmoituksille.

**Vaihtoehtoinen tapa (HTML/AJAX, ei suositella REST:n sijaan):**

```http
POST https://laura.fi/wp-content/themes/jobify-child/ajax.php?wpml_lang=fi
Content-Type: application/x-www-form-urlencoded

action=get_listings&per_page=100&orderby=date&order=desc&page=1&form_data=…
```

### 5.5 Indeed.fi

| API | Tila |
|---|---|
| Job Search API | **Poistettu 5/2021** |
| Publisher API (embed) | Kutsuperusteinen |
| Job Sync API | Vain ATS-kumppaneille (ilmoitusten vienti Indeediin) |

Indeed toimii aggregaattorina Suomessa, mutta **työpaikkadataa ei voi virallisesti hakea ulos**. Testi 20.6.2026: `fi.indeed.com` ja `www.indeed.fi` → **HTTP 403**, captcha/challenge. Testatut kiertotavat (plain curl, selain-UA + `sec-*`-headerit, `cloudscraper`, Playwright headless Chromium) jäivät kaikki blokkiin.

### 5.6 Careerjet.fi

Kansainvälinen aggregaattori, jolla on **Publisher API** kumppaneille:

```http
GET https://search.api.careerjet.net/v4/query
  ?locale_code=fi_FI
  &keywords=kehittäjä
  &location=Helsinki
  &user_ip=...
  &user_agent=...
Authorization: Basic {API_KEY}:
```

- Rekisteröityminen: [careerjet.com/partners](https://www.careerjet.com/partners/api)
- Testi ilman avainta: **HTTP 401** — *"You did not provide an API key"*
- Aggregoi useita lähteitä (Duunitori, yrityssivut jne.) — ei yksinomaan virallista TMT-dataa

### 5.7 LinkedIn Jobs

- Job Posting API: **vain hyväksytyt Talent Solutions -kumppanit** — ei uusia kumppanuuksia avoinna pieniä toimijoita varten
- Työpaikkojen haku ulos: ei virallista API:a; kolmannen osapuolen scraping-palvelut rikkovat käyttöehtoja

---

## 6. Kuntien avoimet rajapinnat

Harvalla kunnalla on erillinen julkisen työpaikka-API. Helsinki ei tarjoa vastaavaa REST-rajapintaa — avoimet paikat hel.fi:ssä rekrytointijärjestelmän kautta. Etsi muita kuntia [avoindata.fi](https://www.avoindata.fi) ja [hri.fi](https://hri.fi) kuntakohtaisesti. **Tämä projekti ei kerää kuntakohtaisia lähteitä MVP:ssä.**

---

## 7. ATS-järjestelmien julkiset työpaikkasyötteet

Monet suomalaiset työnantajat julkaisevat paikkansa omilla urasivuillaan ATS-alustojen kautta. Nämä tarjoavat **ajantasaista dataa** yrityskohtaisesti:

| ATS | Julkinen endpoint | Formaatti | Testattu |
|---|---|---|---|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | JSON | ✅ Wolt: 282 paikkaa, Relex: 26 paikkaa |
| Lever | `GET https://api.lever.co/v0/postings/{site}?mode=json` | JSON | ❌ 404 suomalaisille yrityksille (harva käyttää Leveriä) |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{name}` | JSON | ✅ Supercell: 37 paikkaa |
| Teamtailor | `GET https://{company}.teamtailor.com/jobs.json` | JSON | ❌ 404 — URL-kaava ei toimi (yrityskohtainen domain) |

Teamtailorilla on myös **suora Työmarkkinatori-kanavaintegraatio** julkaisuun ([ohje](https://support.teamtailor.com/en/articles/5267295-publish-jobs-on-tyomarkkinatori)).

---

## 8. Vertailu: mistä saa ajantasaista työpaikkadataa?

```
                    Ajantasaisuus + kattavuus
                         ▲
                         │
    Duunitori jobentries ─┤  ~16,8k, epävirallinen JSON (nopein kaupallinen)
    TMT haku-API  ────────┤  ~11,3k, virallinen työnvälitys (epävirallinen endpoint)
    Laura WP REST  ───────┤  ~12k, aggregaatti (epävirallinen REST)
    EURES (fi)  ──────────┤  ~12k, lähde TMT (virallinen EU-API)
    KIPA P67  ────────────┤  sama data kuin TMT, sopimus + IP (virallinen)
    Jobly sitemap  ───────┤  ~13,8k URL, ei listaa-API:a
    Careerjet  ───────────┤  aggregaatti, vaatii API-avaimen
                         │
    StatFin PxWeb  ───────┤  kuukausittainen aggregaatti (~35k)
    FSD vakanssidata ─────┤  vuosittainen tutkimusaineisto
                         │
    Indeed.fi  ───────────┴  HTTP 403, ei DIY-scrapea
```

---

## 9. Suositukset eri käyttötarkoituksiin

### Tämän projektin keruupino (MVP → laajennus)

Projektin MVP-keruu vastaa [`goal.md`](goal.md)-MVP:tä ja [`sources.yaml`](sources.yaml)-rekisteriä:

1. **MVP-lähteet:** Duunitori `jobentries` + TMT `search/v2/search` + Laura REST
2. **Post-MVP:** Jobly sitemap + JSON-LD; EURES täydentäväksi, ei luotettava incrementaali
3. **Ei MVP:ssä:** Indeed, Careerjet, KIPA P67 (estetty tai sopimusvaatimus)
4. **Incrementaali:** Duunitori `-date_posted`-sivutus, TMT `publishedAfter` (`pageSize=90`), Laura `after`/`modified_after`
5. **Recall-lisähaku:** aja Duunitori/TMT/Laura-incrementaalin rinnalla pieni `DISCOVERY_SEARCH_QUERIES`-termilista. Tämä palauttaa edelleen avoimia, mutta vesileimaa vanhempia korkean arvon osumia kuten kirjastonhoitaja-, kirjastovirkailija-, informaatikko-, musiikkikirjasto-, sisällöntuottaja- ja kulttuurituottajaroolit.

Alla yleisempi vertailu muihin käyttötarkoituksiin.

### Työnhakusovellus / aggregaattori (laaja kattavuus Suomessa)

1. **Nopea aloitus (ei sopimusta):** TMT `search/v2/search` + Duunitori `jobentries` + Laura REST — toimii heti
2. **RSS-tarve:** TMT- tai EURES-API → muunna oma RSS (ei valmista feediä)
3. **Duunitori:** `GET /api/v1/jobentries?ordering=-date_posted` (ensisijainen); sitemap varalle
4. **Laura:** `GET /wp-json/wp/v2/job-listings?after=…` incrementaalipollaukseen
5. **Tuotantokäyttö (virallinen):** KIPA P67 KEHA-sopimuksella
6. **Jobly:** sitemap `lastmod` + yksityissivun JSON-LD
7. **Kaupalliset portaalit:** Careerjet-kumppanuus; scraping vain jos API puuttuu (huomioi ToS)

### Rekrytoiva organisaatio / ATS-integraatio

1. **Ilmoitusten julkaisu:** Työmarkkinatori P66 (hallintarajapinta) KEHA-keskuksen kautta
2. **Jobly.fi:** XML-API ilmoitusten julkaisuun
3. **Valmiit kanavat:** Teamtailor / SAP SuccessFactors integraatiot Työmarkkinatorille

### Tilastot ja analytiikka

1. **StatFin PxWeb** — viralliset kuukausiluvut (CC BY 4.0)
2. **FSD vakanssidata** — historiallinen tutkimus

### Kuntien työpaikat

1. Tarkista avoindata.fi / HRI kuntakohtaisesti — MVP ei sisällä kuntakohtaisia REST-lähteitä

---

## 10. Testausyhteenveto — mikä todella toimii (20.6.2026)

Uudelleentarkistettu curl-testeillä samana päivänä.

### ✅ Toimii ilman sopimusta / avainta

| Palvelu | Metodi / URL | HTTP | Tulos |
|---|---|---|---|
| **TMT haku v2** | POST `tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search` | 200 | **11 327** ilmoitusta |
| **TMT yksittäinen** | GET `.../jobposting-new/v1/public/jobpostings/{uuid}` | 200 | Täysi JSON |
| **TMT koodistot** | GET `.../api/codes/v1/koodistot` | 200 | **211** koodistoa |
| **TMT hakuvahti** | POST `.../watch/v2/register` | 400 | Endpoint OK (validoi syötteen) |
| **EURES haku** | POST `europa.eu/eures/api/.../jv-search/search` | 200 | **12 005** fi-ilmoitusta |
| **EURES yksittäinen** | GET `.../jv/id/{id}` | 200 | `source: TMT` |
| **EURES tilastot** | GET `.../getNumberOfJobs` | 200 | **2 981 298** globaalisti |
| **StatFin PxWeb** | POST `pxdata.stat.fi/.../12r5.px` | 200 | **34 865** avointa (2026M04) |
| **Greenhouse (Wolt)** | GET `boards-api.greenhouse.io/.../wolt/jobs?content=true` | 200 | **282** paikkaa (sis. Helsinki) |
| **Greenhouse (Relex)** | GET `boards-api.greenhouse.io/.../relex/jobs` | 200 | **26** paikkaa |
| **Ashby (Supercell)** | GET `api.ashbyhq.com/posting-api/job-board/supercell` | 200 | **37** paikkaa |
| **Duunitori jobentries** | GET `duunitori.fi/api/v1/jobentries?ordering=-date_posted` | 200 | **16 809** ilmoitusta + `descr` |
| **Laura REST** | GET `laura.fi/wp-json/wp/v2/job-listings` | 200 | **11 997** ilmoitusta |
| **Duunitori OpenSearch** | GET `duunitori.fi/opensearch.xml` | 200 | Hakupohja selaimelle |
| **Duunitori sitemap** | GET `duunitori.fi/sitemap-jobentry.xml` | 200 | Ilmoitus-URL:t + `lastmod` |

### ⚠️ Toimii vain avaimella / sopimuksella

| Palvelu | HTTP | Huomio |
|---|---|---|
| **KIPA P67/P66** | Timeout | IP + API-avain pakollinen |
| **Legacy integraatiot.tyomarkkinatori.fi** | Timeout | v1.0, sama suojaus |
| **Jobly julkaisu-API** | 401 | Vain ilmoittajille (`Invalid key`) |
| **Careerjet** | 401 | Publisher API -avain pakollinen |

### ❌ Ei toimi / ei ole olemassa

| Palvelu | HTTP | Huomio |
|---|---|---|
| TMT haku **v1** | 404 | Korvattu v2:lla |
| TMT watch **v1** | 404 | Korvattu v2:lla |
| Duunitori `/api/jobs` | 404 | Vanha polku; käytä `/api/v1/jobentries` |
| Duunitori `/rss`, `/feed` | timeout | Cloudflare-suojattu; `robots.txt`: `Disallow: /` |
| Työmarkkinatori `/rss`, `/feed` | 404 | Ei RSS:ää |
| Työmarkkinatori `/rss/tyopaikat` | 200 tyhjä XML | Shell-rakenne (CMS localhost), 0 item |
| Jobly.fi `/rss.xml` | 200 HTML | Palauttaa normaalin sivun, ei RSS:ää |
| Jobly listasivut | 404 | `/tyopaikka`, `/tyopaikkailmoitus/hae` |
| Jobly sitemap | 200 | 13 779 työ-URL (page 1+2) |
| EURES `/rss` | timeout/dns | Ei RSS:ää |
| Indeed.fi haku | 403 | Bot-suoja; captcha |
| Laura työpaikka-RSS | 200 tyhjä | WordPress-syöte, 0 item |
| Teamtailor `{company}.teamtailor.com/jobs.json` | 404/dns | URL-kaava ei toimi suomalaisille |
| Lever `api.lever.co/v0/postings/{company}` | 404 | Ei suomalaisia Lever-yrityksiä löydy |

---

## 11. RSS, Atom, sitemapit ja hakuvahdit

**Yhteenveto:** Suomalaisilla valtakunnallisilla työnhakuportaaleilla **ei ole yleistä julkista RSS-syötettä** kaikille avoimille työpaikoille. Lähin vaihtoehto on REST/API (TMT, EURES) tai sitemap (Duunitori).

### 11.1 Työmarkkinatori

| Muoto | URL | Toimii? | Sisältö |
|---|---|---|---|
| RSS / Atom | `/rss`, `/feed`, `/atom.xml` | ❌ 404 | — |
| “RSS-polku” | `/henkiloasiakkaat/avoimet-tyopaikat/rss` | ⚠️ 200 HTML | Tavallinen sivu, ei syötettä |
| “RSS-shell” | `/rss/tyopaikat` | ⚠️ 200 tyhjä XML | RSS-rakenne olemassa mutta 0 item (CMS-jäänne) |
| REST haku | `POST .../search/v2/search` | ✅ | **Paras korvike RSS:lle** |
| Sähköpostihakuvahti | `POST .../watch/v2/register` | ✅ | Sähköposti, ei RSS |
| Sitemap | `/sitemap.xml` | ❌ 404 | — |

### 11.2 EURES

| Muoto | Toimii? | Huomio |
|---|---|---|
| RSS | ❌ | Ei virallista feediä |
| REST API | ✅ | JSON-haku; RSS voidaan rakentaa itse |

### 11.3 Duunitori

| Muoto | URL | Toimii? | Sisältö |
|---|---|---|---|
| **REST jobentries** | `/api/v1/jobentries` | ✅ | JSON, täysi `descr`, **ensisijainen** |
| RSS / Atom | `/rss`, `/feed` | ❌ 404 | — |
| OpenSearch | `/opensearch.xml` | ✅ | HTML-hakulinkki, ei dataa |
| **Sitemap (työpaikat)** | `/sitemap-jobentry.xml` (+ `?p=2`…) | ✅ | URL + `lastmod` (85 sivua × 200 URL) |
| Duunivahti | Web UI | ✅ | Sähköposti, ei RSS |

Duunitorin **jobentries-API** korvaa sitemapin ohjelmallisessa haussa. Sitemap on varajae:

```
https://duunitori.fi/sitemap-jobentry.xml
https://duunitori.fi/sitemap-jobentry.xml?p=2
...
```

### 11.4 Jobly.fi

| Muoto | URL | Toimii? | Sisältö |
|---|---|---|---|
| Hakusivut | `/tyopaikkailmoitus/hae`, `/tyopaikka` | ❌ 404 | Ei staattista listaa |
| Julkaisu-API (XML) | `/api/v1/job?key=…` | ⚠️ 401 | Vain ATS-asiakkaille |
| **Sitemap** | `/sitemap.xml?page=1` (+ page=2) | ✅ | **13 779** työ-URL, `lastmod` |
| Yksityissivu JSON-LD | `/tyopaikka/{slug}-{id}` | ⚠️ | JobPosting osassa ilmoituksista |

### 11.5 Laura.fi / Rekrytointi.com

| Muoto | URL | Toimii? | Sisältö |
|---|---|---|---|
| **WordPress REST** | `/wp-json/wp/v2/job-listings` | ✅ | JSON, 11 997 ilmoitusta |
| `laura.fi/feed/` | WordPress RSS | ✅ tyhjä | 0 item — ei työpaikkoja |
| `laura.fi/avoimet-tyopaikat/feed/` | Kommenttisyöte | ✅ tyhjä | 0 item |
| `rekrytointi.com/rss/` | WordPress RSS | ✅ tyhjä | 0 item (brändi ohjaa Lauraan) |
| **Extended Job Feed** | Asiakaskohtainen | ⚠️ | Vain Laura Rekrytointijärjestelmän asiakkaille |
| Hakuvahti | Web UI | ✅ | Sähköposti |

### 11.6 Kunnat ja julkishallinto

| Lähde | Muoto | Toimii? |
|---|---|---|
| Helsinki | RSS | ❌ 404 |
| HelsinkiRekry | RSS | ❌ HTML-sivu |

Harvalla kunnalla on erillinen avoin työpaikka-API; tämä projekti ei kerää kuntalähteitä MVP:ssä.

### 11.7 Muut vaihtoehdot RSS:n sijaan

| Vaihtoehto | Kuvaus |
|---|---|
| **TMT search v2 → oma RSS** | Muunna JSON JSON:sta RSS:ksi (suositus) |
| **EURES API → oma RSS** | Sama, lähde `TMT` |
| **Duunitori jobentries API** | JSON, incrementaalipollaus `ordering=-date_posted` |
| **Laura REST → oma RSS** | `after=` incrementaalipollaus |
| **Duunitori sitemap → crawl** | Varajae; yksityiskohdat tarvittaessa HTML/JSON-LD |
| **ATS JSON** (Greenhouse, Teamtailor) | Yrityskohtainen, ajantasainen |
| **Sähköpostihakuvahdit** | TMT, Laura, Duunitori — ei integroitava syöte |

---

## 12. Ohjelmallinen keruu — API, sitemap ja scrape (integroitu)

Tämä luku yhdistää rajapintakartoituksen, toimivat curl-esimerkit ja scrape-vs-API-testit **yhdeksi tuotantosuositukseksi**. Tavoite: **mahdollisimman tuoreet ilmoitukset luotettavasti**.

### 12.1 Menetelmävalinta — prioriteettijärjestys

| Prioriteetti | Menetelmä | Milloin | Nopeus | Hauraus |
|---|---|---|---|---|
| 1 | **JSON-API** | Endpoint löytyy ja palauttaa listauksen | ~0,1–0,3 s/req | Keskitaso (epäviralliset endpointit) |
| 2 | **REST / sitemap + JSON-LD** | Ei listaa-API:a, mutta URL + metadata saatavilla | sitemap hitaampi | Matala–keskitaso |
| 3 | **HTML-listaus (scrape)** | API puuttuu, SSR-sivu sisältää linkit | ~22 ilmoitusta/sivu Duunitorilla | Korkea |
| 4 | **Headless-selain** | JS-renderöity lista (Jobly) tai bot-suoja (Indeed) | 10–50× hitaampi | Erittäin korkea |

**Keskeinen johtopäätös:** HTML-scrapeaus **ei ole ensisijainen tapa** suurille suomalaisille portaaleille. Käytä API:ta tai sitemapia.

### 12.2 Lähdekohtainen keruusuositus (testattu 20.6.2026)

| Lähde | Ensisijainen tapa | Endpoint / menetelmä | Ilmoituksia | Incrementaali | Rate limit (testi) |
|---|---|---|---|---:|---|
| **Duunitori** | JSON-API | `GET /api/v1/jobentries?ordering=-date_posted&page_size=100` | 16 809 | `max(date_posted)`, sivut 1–3 | 10/10 @ ~0,3 s |
| **Työmarkkinatori** | JSON-API | `POST .../search/v2/search` + `sorting: LATEST` | 11 327 | `publishedAfter` tai LATEST, `pageSize` max 90 | 10/10 @ ~0,28 s |
| **Laura.fi** | WordPress REST | `GET /wp-json/wp/v2/job-listings?orderby=date&after=…` | 11 997 | `after={iso8601}` | 10/10 @ ~0,13 s |
| **EURES** | JSON-API | `POST .../jv-search/search` | ~12 000 (fi) | ⚠️ `MOST_RECENT` epäluotettava | OK |
| **Jobly** | Sitemap + JSON-LD | `sitemap.xml?page=1|2`, `/tyopaikka/{slug}` | 13 779 | `lastmod`-päivä | sitemap ~30 s |
| **Indeed.fi** | — | ❌ HTTP 403 | — | — | Estetty |
| **ATS (Greenhouse jne.)** | Yritys-API | `boards-api.greenhouse.io/...` | yrityskoht. | full poll | OK |

#### Nopeat curl-esimerkit

**Työmarkkinatori — uusimmat ilmoitukset**

```bash
curl -X POST https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search \
  -H "Content-Type: application/json" \
  -d '{"query":"","filters":{},"paging":{"pageNumber":0,"pageSize":20},"sorting":"LATEST"}'
```

**Työmarkkinatori — incrementaali (`publishedAfter`, `pageSize` max 90)**

```bash
curl -X POST https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search \
  -H "Content-Type: application/json" \
  -d '{"query":"","filters":{"publishedAfter":"2026-06-19T00:00:00.000Z"},"paging":{"pageNumber":0,"pageSize":20},"sorting":"LATEST"}'
```

**Duunitori — uusimmat ilmoitukset**

```bash
curl "https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=50" \
  -H "User-Agent: JobResearch/1.0"
```

**Laura.fi — uusimmat ilmoitukset**

```bash
curl "https://laura.fi/wp-json/wp/v2/job-listings?per_page=10&orderby=date&order=desc"
```

**Laura.fi — incrementaali**

```bash
curl "https://laura.fi/wp-json/wp/v2/job-listings?per_page=100&orderby=date&order=desc&after=2026-06-20T06:00:00"
```

**EURES — Suomen ilmoitukset**

```bash
curl -X POST "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search" \
  -H "Content-Type: application/json" \
  -d '{"resultsPerPage":10,"page":1,"sortSearch":"MOST_RECENT","keywords":[],"locationCodes":["fi"]}'
```

**Jobly — sitemap**

```bash
curl "https://www.jobly.fi/sitemap.xml?page=1"
curl "https://www.jobly.fi/sitemap.xml?page=2"
```

**ATS-esimerkit — yrityskohtaiset urasivut**

```bash
curl "https://boards-api.greenhouse.io/v1/boards/wolt/jobs?content=true"
curl "https://boards-api.greenhouse.io/v1/boards/relex/jobs?content=true"
curl "https://api.ashbyhq.com/posting-api/job-board/supercell"
```

#### Duunitori — `jobentries` (tärkein löydös)

```http
GET https://duunitori.fi/api/v1/jobentries?ordering=-date_posted&page=1&page_size=100
User-Agent: Mozilla/5.0
```

| Kenttä | Kuvaus |
|---|---|
| `count` | Kokonaismäärä (16 809) |
| `results[].heading` | Otsikko |
| `results[].date_posted` | Julkaisuaika (ISO 8601, +03:00) |
| `results[].slug` | URL-polku |
| `results[].company_name` | Työnantaja |
| `results[].municipality_name` | Kunta |
| `results[].descr` | **Koko ilmoituksen teksti** — ei tarvitse erillistä sivuhakua |

| Parametri | Toimii? | Huomio |
|---|---|---|
| `ordering=-date_posted` | ✅ | Uusimmat ensin |
| `page=N`, `page_size=100` | ✅ | Max 100/sivu |
| `page_size>100` | ⚠️ | Hyväksytään, mutta katkaistaan hiljaisesti 100 tulokseen |
| `GET /api/v1/jobentries/{slug}` | ❌ 404 | Ei yksittäishakua |
| `date_posted__gte=…` | ⚠️ | Hyväksytään, ei filtteröi |
| `municipality=…` | ⚠️ | Hyväksytään, ei filtteröi (`count` pysyy samana) |
| `search=…` | ✅ | Avainsanahaku toimii (esim. `search=ohjelmoija`) |

**Sitemap varajae:** 85 sivua × 200 URL (`sitemap-jobentry.xml?p=N`). Tiheä pollaus (<0,2 s) voi aiheuttaa HTTP 403 — preferoi jobentries-API.

#### Työmarkkinatori — pageSize-rajoitukset (kriittinen)

| Kutsu | `pageSize`-havainto |
|---|---|
| `sorting: LATEST` | **90 toimii**; `>=99` → HTTP 400 |
| `filters.publishedAfter` | **90 toimii**; `>=99` → HTTP 400 |
| `sorting: PUBLICATION_TIME_DESC` | ❌ HTTP 400 |

#### Laura — REST vs. HTML

Listasivu `/avoimet-tyopaikat/` palauttaa **0 ilmoitusta** ilman JavaScriptiä (WP Job Manager AJAX). REST on aina parempi:

```http
GET https://laura.fi/wp-json/wp/v2/job-listings?per_page=100&orderby=date&order=desc&after=2026-06-20T06:00:00
```

Headerit: `X-WP-Total: 11997`, `X-WP-TotalPages: 120` (kun `per_page=100`).

**Laura incrementaali — kaksi eri parametria:**

| Parametri | Käyttö | Huomio |
|---|---|---|
| `after=` | Uudet julkaisut | WordPress publish date; suositeltu 5 min pollaukseen |
| `modified_after=` | Muokatut ilmoitukset | Laajempi joukko (sis. vanhojen päivitykset); ei korvaa `after=` |

#### Jobly — vain sitemap toimii ilman JS:ää

| URL | HTTP | Tulos |
|---|---|---|
| `/tyopaikkailmoitus/hae` | 404 | Ei listaa |
| `/tyopaikka` | 404 | Ei listaa |
| `/tyopaikka/{slug}-{id}` | 200 | JSON-LD JobPosting (osassa) |
| `/sitemap.xml?page=1` | 200 | 9 569 työ-URL |

### 12.3 Tuoreusvertailu (snapshot 20.6.2026 ~13 UTC)

Top-5 uusinta — **lähteet eivät ole identtisiä**:

| # | Duunitori API | TMT LATEST | Laura REST |
|---|---|---|---|
| 1 | Tarjoilijoita, kesäsesonki (13:00) | Ratsastustuntien pitäjä (07:00) | Ratsastustuntien pitäjä (07:00) |
| 2 | Pizza Crew (12:00) | Kirvesmies (05:42) | Kirvesmies (05:42) |
| 3 | BDM Nordics, Eaton (08:21) | Lähihoitaja (18.6.) | Parturi (19.6.) |
| 4 | Site Supervisor, Eaton (08:21) | Raudoittaja (18.6.) | Personal assistant (19.6.) |
| 5 | Lähihoitaja Kaarina (08:00) | LVI-asentaja (18.6.) | Online Solution Expert (19.6.) |

**Tulkinta:**

- **Duunitori** sisältää ilmoituksia, joita TMT:n top-listassa ei vielä ole (Eaton, Mustion Linna) → **pakollinen erillinen lähde**
- **Laura** matchaa TMT:n ylimmässä listassa; alempana poikkeaa
- **Dedup pakollinen** — sama työ 2–3 lähteessä eri otsikolla/ajalla

### 12.4 Suositeltu keruuputki (tuotanto)

```
Scheduler (5 min)
    ├── Duunitori jobentries  page 1–3, ordering=-date_posted
    ├── TMT search/v2         LATEST, pageSize 90
    └── Laura REST            orderby=date, after={watermark}
              │
              ▼
        Normalisointi + deduplikointi
        (title + employer + municipality, fuzzy)
              │
              ▼
        Tietokanta (source_id, slug, uuid per lähde)

Scheduler (30–60 min)
    └── Jobly sitemap lastmod-filter → JSON-LD fetch uusille URL:ille
```

| Lähde | Watermark | Pollausväli |
|---|---|---|
| Duunitori | `max(date_posted)` | 5 min |
| TMT | `max(created)` tai `publishedAfter` | 5 min |
| Laura | `after={iso}` | 5 min |
| Jobly | `max(lastmod)` sitemapista | 30–60 min |
| EURES | varovasti `max(creationDate)` | 15 min (täydentävä) |

**User-Agent:** Aseta tunnistettava merkkijono (esim. `JobAggregator/1.0 (+https://example.com/contact)`).

#### Ensimmäinen täysi synkronointi (backfill)

| Lähde | Sivuja | Arvio @ 5 s/req | Huomio |
|---|---:|---:|---|
| Duunitori | ~168 | ~14 min | `page_size=100`, `count`/169 sivua |
| TMT | ~126 | ~10,5 min | `pageSize=90`, `LATEST`, `totalElements`/90 |
| Laura | 120 | ~4 min | `per_page=100`, `X-WP-TotalPages` |
| Jobly | 2 sitemap + N fetch | ~30 min+ | sitemap nopea; JSON-LD per URL hidastaa |

Incrementaalipollaus (5 min) hakee vain uudet sivut 1–3 / `publishedAfter` / `after=` — älä full-crawlaa jatkuvasti. Tämän rinnalle kannattaa kuitenkin ajaa pieni kohdennettu avainsanahaku tärkeille rooliperheille, koska ajantasainen avoin ilmoitus voi olla vesileimaa vanhempi.

### 12.5 HTML-scrapeaus — milloin sallittu / pakko

| Skenaario | Scrape? | Parempi vaihtoehto |
|---|---|---|
| Duunitori uusimmat | ❌ | jobentries API |
| Laura listaus | ❌ | WP REST |
| Jobly listaus | ❌ | sitemap |
| Jobly yksityiskohta | JSON-LD | headless jos schema puuttuu |
| TMT | ❌ | search/v2 API |
| Indeed | ❌❌ | Partner API |
| Pienet yrityssivut | ✅ | schema.org / HTML |

Duunitorin HTML-hakusivu (`/tyopaikat?sort=published`) palauttaa vain **~22 uniikkia ilmoitusta** staattisessa HTML:ssä — riittämätön.

### 12.6 Python-esimerkit

#### Duunitori — uusimmat ilmoitukset

```python
import requests


def fetch_duunitori_newest(page=1, page_size=50):
    r = requests.get(
        "https://duunitori.fi/api/v1/jobentries",
        params={"ordering": "-date_posted", "page": page, "page_size": page_size},
        headers={"User-Agent": "JobResearch/1.0"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    jobs = []
    for item in data["results"]:
        jobs.append({
            "source": "duunitori",
            "title": item["heading"],
            "employer": item["company_name"],
            "location": item["municipality_name"],
            "posted_at": item["date_posted"],
            "slug": item["slug"],
            "url": f"https://duunitori.fi/tyopaikat/tyo/{item['slug']}",
            "description": item.get("descr", ""),
        })
    return jobs, data["count"]
```

#### Työmarkkinatori — uusimmat ilmoitukset

```python
import requests


def _tmt_title(title_obj: dict) -> str:
    return title_obj.get("fi") or title_obj.get("en") or next(iter(title_obj.values()), "")


def fetch_tmt_newest(page_size=90, max_pages=5):
    jobs = []
    for page_num in range(max_pages):
        r = requests.post(
            "https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search",
            json={
                "query": "",
                "filters": {},
                "paging": {"pageNumber": page_num, "pageSize": page_size},
                "sorting": "LATEST",
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        for j in data["content"]:
            jobs.append({
                "source": "tmt",
                "id": j["id"],
                "title": _tmt_title(j.get("title", {})),
                "employer": j.get("employer", {}).get("name"),
                "posted_at": j["created"],
            })
        if data.get("last", False) or not data.get("content"):
            break
    return jobs
```

#### Laura — incrementaalipollaus

```python
import requests


def fetch_laura_since(iso_timestamp: str):
    r = requests.get(
        "https://laura.fi/wp-json/wp/v2/job-listings",
        params={
            "per_page": 100,
            "orderby": "date",
            "order": "desc",
            "after": iso_timestamp,
        },
        timeout=20,
    )
    r.raise_for_status()
    return [{
        "source": "laura",
        "id": j["id"],
        "title": j.get("title", {}).get("rendered", ""),
        "posted_at": j["date_gmt"],
        "url": j["link"],
    } for j in r.json()]
```

#### Jobly — sitemap incrementaali

```python
import re
from datetime import date

import requests


def jobly_urls_updated_since(day: str | None = None):
    day = day or str(date.today())
    urls = []
    for page in (1, 2):
        xml = requests.get(
            f"https://www.jobly.fi/sitemap.xml?page={page}",
            headers={"User-Agent": "JobResearch/1.0"},
            timeout=60,
        ).text
        for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
            if "/tyopaikka/" not in block:
                continue
            loc = re.search(r"<loc>([^<]+)</loc>", block).group(1)
            mod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            mod = mod.group(1) if mod else ""
            if mod.startswith(day):
                urls.append((mod, loc))
    return sorted(urls, reverse=True)
```

### 12.7 Testilogi (uudelleenverifiointi 20.6.2026)

| Testi | Tulos |
|---|---|
| TMT LATEST | ✅ 11 327 |
| TMT publishedAfter | ✅ pageSize 90; ❌ pageSize ≥99 → 400 |
| TMT LATEST pageSize | ✅ pageSize 90; ❌ pageSize ≥99 → 400 |
| TMT otsikon kielifallback | ✅ `fi → en → mikä tahansa` (`fi` puuttuu osasta ilmoituksia) |
| TMT paginointi | ✅ `fetch_tmt_newest` käy sivut läpi ja pysähtyy `last`/tyhjään sisältöön |
| Duunitori jobentries | ✅ 16 809, `descr` mukana |
| Duunitori page_size-katto | ✅ `page_size=200` palauttaa 100 tulosta ilman virhettä |
| Duunitori jobentries/{slug} | ❌ 404 |
| Duunitori sitemap | ✅ 85×200 URL |
| Laura REST | ✅ 11 997, TotalPages=120 |
| Laura RSS | ❌ tyhjä |
| Jobly sitemap | ✅ 13 779 URL |
| Jobly sitemap lastmod tänään | ✅ 77 URL (sivu 1: 31, sivu 2: 46) |
| Jobly listasivut | ❌ 404 |
| Jobly JSON-LD | ⚠️ osassa ilmoituksista |
| EURES minimal payload | ✅ `numberRecords`, `jvs` |
| EURES MOST_RECENT sort | ⚠️ epäjohdonmukainen |
| Indeed.fi | ❌ 403 |
| Rate limit TMT/Laura/jobentries | ✅ 10/10 |

Toistettavat skriptit: [`../scrape-test/`](../scrape-test/). Regressiovartija: `make test-regression`.

---

## 13. Research: Kuntarekry, Valtiolle.fi, and TMT regional pages

**Verified:** 2026-06-20 (live HTTP probes, stdlib + JS bundle inspection)

This section records a phased research pass on three related public-sector job surfaces that are **not** in the MVP collector yet. Goal: determine whether each surface exposes an extensive, machine-harvestable feed comparable to Duunitori/TMT/Laura.

### 13.1 Kuntarekry (`kuntarekry.fi`)

**Role:** National municipal and wellbeing-services county job portal (Talentech / Grade Solutions ProcessWire stack).

| Check | Result |
|---|---|
| Public documented API | ❌ No OpenAPI or partner docs for anonymous harvest |
| `robots.txt` sitemap | ✅ HTML sitemaps at `/fi/sivukartta/` and `/se/sitemap/` — **no per-job URL list** |
| Guessed REST paths (`/api/jobs`, `/rss`, `/feed`) | ❌ HTTP 404 |
| **`?format=json` on listing URL** | ✅ **Works** — returns JSON array of job summaries |
| Pagination (`page`, `offset`, `limit`) | ❌ **Broken for harvest** — see below |
| Filter query params (`locations`, `profit_center`, `q=Oulu`) | ❌ **Ignored** when using `format=json` with plural param names |
| **`organisation={id}` filter** (singular) | ✅ **Works** — returns that employer’s jobs (not the global 48-row cap set) |
| **`GET /fi/api/filters-data/`** | ✅ Public JSON — **571** employer IDs + location/profession trees (metadata only) |
| Detail page `?format=json` | ❌ Returns HTML, not JSON |
| Employer RSS/XML feeds | ⚠️ Documented for **registered employers only** ([Kuntarekry for employers](https://www.kuntarekry.fi/en/for-employers/)), not a public aggregate feed |

**Listing JSON endpoints (epävirallinen):**

```http
GET https://www.kuntarekry.fi/fi/tyopaikat/?format=json&organisation={id}
GET https://www.kuntarekry.fi/fi/api/filters-data/
GET https://www.kuntarekry.fi/fi/tyopaikat/?view=count&format=json   → {"count": N}
```

Use **`organisation`** (singular), not `organisations`. Employer IDs come from `filters-data.organisations`.

**Response shape (array item):**

```json
{
  "id": 1898270,
  "name": "kesatyo-lahihoitaja-...",
  "title": "Kesätyö, Lähihoitaja, ...",
  "url": "/fi/tyopaikat/kesatyo-lahihoitaja-.../",
  "profit_center": "Varsinais-Suomen hyvinvointialue",
  "publication_date": "20.6.2026",
  "publication_end": "3.7.2026",
  "ext_id": "22378",
  "published": "24h"
}
```

**Volume verification (20.6.2026, spike pass 2):**

| Probe | Result |
|---|---|
| HTML UI advertises | **~1 900** avointa työpaikkaa (page copy; earlier probe saw ~4 000) |
| `?format=json` default page | **48** jobs (hard cap per response) |
| `?format=json&page=2…375` | Same IDs repeat — pagination broken |
| **Sort sharding** (5 sort orders merged) | **101** unique IDs |
| **Regional path shards** (`/fi/tyopaikat/{oulu,helsinki,...}?format=json`) | **278** unique IDs (8 cities) |
| **`organisation={id}` sweep** via `/fi/api/filters-data/` (571 orgs) | **1 158** unique IDs, 225 orgs with ≥1 job (~289 s, stdlib) |
| Playwright DOM link harvest | **51** anchors — no extra JSON job API observed |

**Front-end architecture:** Single-page job search uses Lit web components (`job-list`, `job-search-tags`) and fetches `window.location.pathname + window.location.search` expecting `{ jobs: [], total: N }` in some code paths (`Vu()` parser in `/dist/js/app.eQ6lcP.js`). Server-side `?format=json` returns a **bare array** without `total` and does not honour `page` — so **stdlib HTTP cannot walk the full ~4k catalog today**.

**Harvest assessment (updated after org-shard spike):**

| Strategy | Extensive results? | Notes |
|---|---|---|
| `?format=json` + pagination | ❌ | Same 48 rows only |
| Filter sharding (`organisations`, `locations`, `q`) | ❌ | Plural param names ignored |
| **Organisation sharding (`organisation={id}`)** | ✅ **Partial** | **1 158 / ~1 900** (~61 %); ~571 HTTP calls, no browser |
| Sort sharding (5 orders) | ⚠️ | **101** unique — useful supplement only |
| Regional path shards | ⚠️ | **278** unique across 8 cities — good for geo-focused poll |
| Playwright / browser | ⚠️ | Renders ~51 links; no hidden bulk JSON API found in network capture |
| Employer RSS/XML feeds | ⚠️ | Registered employers only — not aggregate harvest |
| TMT + Laura overlap | ⚠️ Partial | Many municipal postings also on TMT; Kuntarekry adds ATS-only postings |

**Recommendation:** **Add stdlib adapter using organisation sharding** — poll `/fi/api/filters-data/` then `?format=json&organisation={id}` for each employer, dedupe by `id`. Accept ~60 % national coverage unless Playwright or Talentech contract closes the gap. For Oulu profile, combine with **TMT `municipalities: ["564"]`** (566 jobs) and regional path shard `oulun-kaupunki`.

---

### 13.2 Valtiolle.fi

**Role:** State employer job portal ([Avoimet työpaikat](https://valtiolle.fi/fi/tyopaikat/)). Same Grade/Talentech ProcessWire + Lit component stack as Kuntarekry.

| Check | Result |
|---|---|
| Public REST/API docs | ❌ |
| **`?format=json` on `/fi/tyopaikat/`** | ✅ Same array shape as Kuntarekry (identical field set) |
| Pagination / filters | ❌ `page` repeats; plural filters ignored (`organisations`, `locations`) |
| **`organisation={id}` filter** (singular) | ✅ Per-employer job lists via IDs from `/fi/api/filters-data/` |
| `/fi/api/search-guard/` | ❌ **Not a search API** — saves email job-alert guards (POST form), returns HTTP 400 for JSON probes |
| `/fi/api/filters-data/` | ✅ **159** employer IDs (Valtiolle) |
| Advertised open jobs | **~181** ([työpaikat tyypin mukaan](https://valtiolle.fi/fi/tyopaikat-tyypin-mukaan/), 20.6.2026) |
| `?format=json` harvest (single call) | **48** jobs only |
| **Organisation sweep (159 orgs)** | **187** unique IDs — **~103 %** of advertised count (20.6.2026) |
| Sort sharding (5 orders) | **90** unique IDs |

**Listing JSON endpoints (epävirallinen):**

```http
GET https://valtiolle.fi/fi/tyopaikat/?format=json&organisation={id}
GET https://valtiolle.fi/fi/api/filters-data/
GET https://valtiolle.fi/fi/tyopaikat/?view=count&format=json   → {"count": 189}
```

**TMT overlap check (state jobs are published via Työmarkkinatori pipeline):**

| TMT `query` | `totalElements` (20.6.2026) |
|---|---|
| `Valtiolle` | 49 |
| `valtion` | 49 |
| `ministeriö` | 10 |
| `virasto` | 13 |

TMT full-text search **does not cover all ~181 Valtiolle.fi listings** — many state jobs are on the portal without being findable via simple TMT keyword search, or use different title/employer wording.

Official redistribution path for structured state data remains **TMT search API** (already in collector) or **KIPA P67** (contract). Valtiolle.fi is a **presentation layer**, not a second authoritative API.

**Harvest assessment:** **Add stdlib adapter with organisation sharding** — same pattern as Kuntarekry. Full Valtiolle catalog (~187 jobs) reachable in ~50 s with ~159 HTTP calls. TMT keyword search remains incomplete (~49 for `Valtiolle`); org-sharded Valtiolle harvest is the efficient stdlib path until KIPA P67 contract.

**Online alternatives checked:** Talentech TR Job Portal API ([developer.talentech.io](https://developer.talentech.io/)) requires credentials; employer RSS/XML is login-only; Apify Työmarkkinatori scrapers wrap the TMT API we already use; no public third-party Kuntarekry aggregate feed found.

---

### 13.3 Työmarkkinatori regional page — Oulu example

**URL researched:** [Työpaikat Oulu — aluesivu](https://tyomarkkinatori.fi/aluesivu/oulu/tyopaikat?m=564)

This is a **CMS regional landing page**, not a separate backend. The `m=564` query parameter is the **Työmarkkinatori municipality code for Oulu** (same code family as TMT search filters).

| Check | Result |
|---|---|
| Separate regional JSON API | ❌ None found on page |
| Embedded job count in HTML | ❌ No `totalElements`; page is marketing + filters + links |
| **`POST .../search/v2/search` with `filters.municipalities: ["564"]`** | ✅ **566 jobs** (20.6.2026) |
| Pagination (`pageNumber` 0 vs 1, `pageSize=90`) | ✅ **90 + 90 rows, zero overlap** — full walk feasible (~7 pages) |
| Region filter `regions: ["17"]` (Pohjois-Pohjanmaa) | ✅ **941 jobs** — broader than Oulu municipality |

**Recommended harvest (already supported by TMT adapter):**

```bash
curl -X POST 'https://tyomarkkinatori.fi/api/jobpostingfulltext/search/v2/search' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "",
    "filters": { "municipalities": ["564"] },
    "paging": { "pageNumber": 0, "pageSize": 90 },
    "sorting": "LATEST"
  }'
```

**Recommendation:** **No new source adapter.** Regional aluesivu URLs are UX entry points. For Oulu-focused collection, extend TMT polling with `municipalities: ["564"]` (or `regions: ["17"]` for the wider employment area). The existing TMT collector already covers national search; regional filtering is a **query configuration**, not a new scraper.

---

### 13.4 Oulu municipal pack (verified 2026-06-20)

Oulu-area sources probed after Kuntarekry was queued for adapter work. **Do not add `ouka.fi` as its own source** — it is a curated embed of Kuntarekry URLs.

#### 13.4.1 Oulun kaupunki — `ouka.fi/avoimet-tyopaikat`

| Check | Result |
|---|---|
| Page | ✅ HTTP 200 — Drupal page “Oulun kaupungin avoimet työpaikat” |
| Listing mechanism | Hard-coded **Kuntarekry** links (`https://kuntarekry.fi/fi/tyopaikat/...`) |
| RSS / Atom (`/feed`, `/rss`) | ❌ HTTP 404 |
| Drupal JSON API | ❌ HTTP 404 / 406 |
| Unique value vs Kuntarekry | ❌ Subset only (38 links vs Kuntarekry city SSR 28; 6 ouka-only slugs still on Kuntarekry) |

**Recommendation:** **Skip.** Covered when Kuntarekry adapter harvests `oulun-kaupunki` and regional shards.

#### 13.4.2 Oulun yliopisto — Varbi ATS

| Check | Result |
|---|---|
| Employer careers page | `https://www.oulu.fi/fi/avoimet-tyopaikat` — HTML table linking to Varbi |
| Varbi listing HTML | ✅ `https://oulunyliopisto.varbi.com/fi/` — **15** job IDs in SSR |
| **Varbi RSS** | ✅ `https://oulunyliopisto.varbi.com/fi/what:rssfeed/` — **15** `<item>` rows |
| Varbi guessed JSON/listing API | ❌ `/what:jobs`, `/what:listing` → HTTP 404 |
| Detail page | ✅ `https://oulunyliopisto.varbi.com/fi/what:job/jobID:{id}/` — full HTML ~96 KB |
| JSON-LD JobPosting | ❌ Not observed |

**Recommended harvest (stdlib, no browser):**

```
1. Poll RSS  → https://oulunyliopisto.varbi.com/fi/what:rssfeed/
2. For each <item>: map jobID from <link> (RSS uses /en/ path)
3. Fetch FI detail HTML for description + deadline
4. Watermark on max(pubDate)
```

RSS is the **primary** path; listing HTML is a fallback checksum. Poll interval **60 min** is sufficient (low volume).

#### 13.4.3 Oulun seurakunnat — Kirkkorekry (not Kuntarekry)

Parish recruitment uses **Kirkkorekry** (`kirkkorekry.fi`), same Talentech/ProcessWire stack as Kuntarekry.

| Check | Result |
|---|---|
| Parish rekry landing | `https://www.oulunseurakunnat.fi/tietoa-meista/rekry/` → links to Kirkkorekry Oulu |
| Oulu regional JSON | ✅ `GET .../fi/tyopaikat/oulu?format=json` → **5** jobs (complete; `page=2` repeats) |
| Pohjois-Pohjanmaa JSON | ✅ `.../fi/tyopaikat/pohjois-pohjanmaa?format=json` → **10** jobs |
| National JSON pagination | ❌ Same broken behaviour as Kuntarekry (`page` repeats) |
| Detail `?format=json` | ❌ Returns HTML |
| `evl.fi` aggregate pages | Marketing WordPress; no machine-readable job list |

**Sample regional JSON item** (identical shape to Kuntarekry):

```json
{
  "id": 296375,
  "title": "Kappalainen Tuiran seurakuntaan",
  "url": "/fi/tyopaikat/kappalainen-tuiran-seurakuntaan-5061/",
  "profit_center": "Tuiran seurakunta",
  "publication_date": "18.6.2026",
  "publication_end": "24.7.2026"
}
```

**Recommended harvest:**

```
1. Poll regional JSON shards (start: oulu, pohjois-pohjanmaa)
2. Dedup by id across shards
3. Fetch detail HTML for full description (summary JSON lacks body text)
4. Watermark on publication_date + id set
```

For the target job-seeker profile, Kirkkorekry is **high value** (kappalainen, hallintosihteeri, seurakuntayhtymä roles) and **not** fully covered by municipal Kuntarekry harvest alone.

#### 13.4.4 Oulu implementation order

| Priority | Source | Adapter type | Why |
|---|---|---|---|
| 1 | **Kuntarekry** | stdlib org-shard JSON + detail HTML | **1 158** national (~61 %); municipal ATS bulk |
| 2 | **Valtiolle** | stdlib org-shard JSON + detail HTML | **187** state jobs — complete via org loop |
| 3 | **TMT `municipalities: ["564"]`** | Config on existing `TmtAdapter` | 566 Oulu jobs via official API |
| 4 | **Kirkkorekry regional** | stdlib JSON + detail HTML | Parish/church roles for Oulu/Lappi |
| 5 | **Oulu Varbi** | RSS + detail HTML | University roles; complete RSS catalog |
| — | **ouka.fi** | — | Skip (Kuntarekry mirror) |

---

### 13.5 Summary table

| Surface | Extensive harvest via stdlib HTTP? | Snapshot volume | Collector action |
|---|---|---:|---|
| **Kuntarekry** | ✅ org-shard stdlib (`organisation={id}`) | **1 158 / ~1 900** (~61 %) | `kuntarekry` adapter — filters-data + org loop + detail HTML |
| **Valtiolle.fi** | ✅ org-shard stdlib | **187 / ~181** | `valtiolle` adapter — same pattern; TMT overlap partial |
| **TMT aluesivu Oulu (`m=564`)** | ✅ via existing TMT API | **566** (Oulu municipality) | `tmt_oulu` adapter — `municipalities: ["564"]` |
| **ouka.fi avoimet työpaikat** | ❌ (Kuntarekry embed only) | 38 curated links | **Skip** — duplicate of Kuntarekry |
| **Oulu Varbi (`oulunyliopisto.varbi.com`)** | ✅ RSS + detail HTML | **15** | `oulu_varbi` adapter |
| **Kirkkorekry Oulu / Pohjois-Pohjanmaa** | ✅ regional `?format=json` | **5** / **10** | `kirkkorekry` adapter — regional shards + detail HTML |

---

## 14. Linkit ja yhteystiedot

| Resurssi | URL |
|---|---|
| Työmarkkinatori rajapinnat | https://tyomarkkinatori.fi/ohjeet-ja-tuki/rajapinnat |
| P67 tekninen dokumentaatio | https://tyomarkkinatori.fi/jobpostingprovider/documentation/KIPA-search-jobpostings-fi.html |
| P67 OpenAPI YAML | https://tyomarkkinatori.fi/jobpostingProvider/swagger/api-docs/P67-tmt-provider-haku-V2 |
| P66 tekninen ohje | https://tyomarkkinatori.fi/jobposting-new/documentation/KIPA-manage-jobpostings-en.html |
| Käyttöönottoilmoituslomake | https://link.webropolsurveys.com/Participation/Public/675df7cd-3222-48a0-bc8d-f089b95315a5?displayId=Fin3601513 |
| KEHA rajapinnat (sähköposti) | tmt-rajapinnat@keha-keskus.fi |
| EURES | https://eures.europa.eu |
| EURES API (epävirallinen dokumentaatio) | https://github.com/rorar/EURES-API-Documentation |
| StatFin työnvälitystilasto | https://stat.fi/fi/tilasto/tyonv |
| Jobly API | https://jobly.almamedia.fi/api/ |
| Duunitori jobentries API | https://duunitori.fi/api/v1/jobentries?ordering=-date_posted |
| Laura REST | https://laura.fi/wp-json/wp/v2/job-listings |
| Duunitori työpaikkasitemap | https://duunitori.fi/sitemap-jobentry.xml |
| Duunitori OpenSearch | https://duunitori.fi/opensearch.xml |
| Oikotie Asunnot API | https://docs.asunnot.oikotie.fi/listing-api/ |
| Careerjet partners | https://www.careerjet.com/partners/api |
| Alma Career integraatiot | https://integrations.almacareer.com/ |
| Oulun kaupunki avoimet työpaikat | https://www.ouka.fi/avoimet-tyopaikat |
| Oulun yliopisto Varbi RSS | https://oulunyliopisto.varbi.com/fi/what:rssfeed/ |
| Kirkkorekry (Oulu) | https://kirkkorekry.fi/fi/tyopaikat/oulu?format=json |
| Oulun seurakunnat rekry | https://www.oulunseurakunnat.fi/tietoa-meista/rekry/ |

---

## 14. Huomioita vastuullisesta käytöstä

1. **Virallinen vs. epävirallinen:** TMT:n verkkosivun haku-API, Duunitorin jobentries ja Laura REST ovat teknisesti avoimia mutta **eivät virallisesti dokumentoituja** integraatioihin. Tuotantoon: **KIPA P67** KEHA-sopimuksella.
2. **Lähdemerkintä:** KEHA edellyttää: *"Lähde: Työmarkkinatorin asiakastietojärjestelmä"* TMT-dataa käytettäessä.
3. **Duunitori:** `robots.txt` sisältää `Disallow: /`. jobentries-API on teknisesti saavutettavissa; noudata kohtuullista pollausväliä (≥3 s) ja käyttöehtoja.
4. **Scraping vs. API:** Preferoi aina JSON-API (TMT, Duunitori jobentries, Laura REST) ennen HTML-scrapeausta. Katso [luku 12](#12-ohjelmallinen-keruu--api-sitemap-ja-scrape-integroitu).
5. **Henkilötiedot:** Työnhakuprofiilit = GDPR. Kerää vain julkisia ilmoituksia.
6. **Kuormitus:** Älä full-crawlaa jatkuvasti; käytä incrementaalia (`publishedAfter`, `after=`, `ordering=-date_posted`, sitemap `lastmod`).
7. **Dedup:** Sama ilmoitus voi esiintyä TMT:ssä, Laurassa, Duunitorilla ja EURES:ssa — normalisoi ennen tallennusta.

---

*Tutkimus tehty curl-testeillä, RSS/sitemap-kartoituksella, OpenAPI-spesifikaatioita lukemalla ja julkisen dokumentaation perusteella. Rajapintojen saatavuus ja ehdot voivat muuttua — tarkista aina viralliset lähteet ennen tuotantointegraatiota.*
