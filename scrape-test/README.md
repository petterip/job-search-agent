# Scrape- ja API-testit

Toistettavat testit työnhakulähteille. Ajettu 20.6.2026 WSL2-ympäristössä.

| Skripti | Tarkoitus |
|---|---|
| `01_suojaukset.sh` | Plain curl ilman UA:ta — HTTP-status, CF/challenge |
| `02_browser_headers.sh` | Selaimen UA + headerit |
| `03_duunitori_analyysi.sh` | Duunitori HTML + **jobentries-API** |
| `04_duunitori_parse.py` | Duunitori SSR-parse + API-endpoint kokeilu |
| `05_apis_testi.sh` | Duunitori, Laura, Jobly API-smoke test |
| `06_playwright_testi.py` | Headless-selain (Duunitori, Jobly, Indeed, LinkedIn) |
| `07_stealth_cloudscraper.py` | Cloudscraper vs. curl (Indeed, Duunitori) |
| `08_tuoreus_testi.sh` | Tuoreus- ja incrementaaliparametrien kokeilu |
| `09_incremental_testi.py` | Inkrementaalistrategiat (Duunitori, Laura, TMT) |
| `10_eures_testi.sh` | EURES API -smoke test |
| `regression_test.py` | **Regressiovartija** — ajettava CI:ssä ja ennen muutoksia |
| `lib/http_client.py` | Jaettu HTTP-apu (stdlib) |

**Dokumentaatio:** [`../docs/tyonhaku-rajapinnat.md`](../docs/tyonhaku-rajapinnat.md) luku 12 · [`../docs/sources.yaml`](../docs/sources.yaml)

```bash
# Regressiovartija (suositus)
make test-regression

cd scrape-test
bash 05_apis_testi.sh          # nopea smoke
bash 10_eures_testi.sh
python3 09_incremental_testi.py
# Playwright: pip install -r ../requirements-dev.txt && playwright install chromium
python3 06_playwright_testi.py
```
