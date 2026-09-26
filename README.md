# Bright Data Web Unlocker provider

Extraction-only Hermes web provider for protected public pages. Routes each URL
through the most specific backend that Bright Data exposes:

1. **Dataset API** (`/datasets/v3/scrape`) for ready-made scrapers identified
   by a `gd_...` dataset id. Useful when Bright Data maintains a structured
   collector for the target site (Wildberries, Ozon, LinkedIn, etc.). On
   success the response is returned as compact JSON; on an empty/error record
   the provider falls back to the generic Web Unlocker.
2. **DCA custom collector** (`/dca/trigger_immediate`) for ad-hoc `c_...`
   collectors you have built in Scraper Studio.
3. **Generic Web Unlocker** (`/request`) as the safety net for any URL.

## Install

```bash
hermes plugins install mawlic/brightdata-unlocker
```

The plugin reads `BRIGHTDATA_API_KEY` from `~/.hermes/.env` (set this once
during Hermes setup). No other credentials are needed.

## Configuration

```yaml
plugins:
  enabled:
    - brightdata_unlocker
web:
  extract_backend: brightdata-unlocker
  brightdata_unlocker:
    zone: web_unlocker1
    render: true
    timeout: 120
    max_concurrency: 3
    max_attempts: 2
    retry_delay: 1.0
    data_format: markdown

    # Ready-made Bright Data scrapers. Pattern keys are matched against the
    # concatenation of hostname + path; the longest match wins. Use `re:` to
    # switch into regex matching.
    datasets:
      www.ozon.ru/product/: gd_lutq85sl13rlndbzai
      www.wildberries.ru/catalog/: gd_luz4fboh2dicd27hhm

    dataset_timeout: 600         # 30..900 seconds
    dataset_poll_interval: 5     # 0.5..60 seconds

    # Custom Scraper Studio collectors.
    collectors:
      market.yandex.ru/card/: c_example_market
      www.aviasales.ru/search/: c_example_aviasales
      're:^www\.avito\.ru/.+_\d+$': c_example_avito
```

The plugin also accepts the `datasets` and `collectors` values as JSON
strings, which keeps the schema stable under strict YAML parsers. The
plugin reads only the values it recognises and ignores other settings.

## What the response looks like

```json
{
  "url": "https://www.wildberries.ru/catalog/1141992971/detail.aspx",
  "title": "Восстановленный Wi-Fi роутер Viva (KN-1913), отличн KEENETIC 1141992971",
  "content": "...",
  "raw_content": "",
  "metadata": {
    "backend": "brightdata-unlocker",
    "dataset_id": "gd_luz4fboh2dicd27hhm",
    "structured": true
  }
}
```

When the Dataset API path fails but the generic Web Unlocker succeeds, the
response metadata includes `dataset_fallback_error` so you can see why the
structured route was skipped.

## Verified target sites (web_unlocker1, 2026-08-01)

| Site | Backend | Live result |
| --- | --- | --- |
| `www.wildberries.ru/catalog/<id>/detail.aspx` | generic Web Unlocker | ~2.5 MB HTML in ~60 s; price, rating, review count visible |
| `www.ozon.ru/product/<slug>-<id>/?oos_search=false` | generic Web Unlocker | ~950 KB HTML in ~25 s; title and reviews visible |
| `www.avito.ru/<city>/<category>?q=...` | generic Web Unlocker | ~1.5 MB HTML in ~12 s; listing prices visible |
| `market.yandex.ru/card/...` | custom collector `c_…` | requires the collector to exist in your Scraper Studio |

## Known free-tier restrictions

- `country: RU` is rejected with `policy_20230`. Web Unlocker therefore runs
  with the default exit (no explicit country). This still works for
  Wildberries, Ozon and Avito public pages; do not assume geo-targeted
  retrieval.
- Bright Data's Wildberries Dataset API (`gd_luz4fboh2dicd27hhm`) and Ozon
  Dataset API (`gd_lutq85sl13rlndbzai`) currently fail with
  `wait_element_timeout` on the residential proxy even for valid product
  URLs; the provider records this in `metadata.dataset_fallback_error` and
  the generic Web Unlocker takes over.
- The Dataset API path is kept enabled because future zone / dataset
  updates may switch behaviour; in the meantime it is best-effort, not
  critical.
- `travel.yandex.ru`, `avia.yandex.ru`, `hotels.yandex.ru` return
  `invalid_path: this endpoint is not supported` from Web Unlocker. They
  keep going through Firecrawl / Browser Use inside the upstream
  `resilient-extract` chain.
- `www.rzd.ru`, `www.aeroflot.ru`, `www.aviasales.ru` are likewise blocked
  by Web Unlocker with `Country RU is not permitted for targeting`. They
  keep going through Firecrawl / Browser Use.

## Tests

```bash
pytest -q tests/test_provider.py
```

Pure-Python tests with `httpx.MockTransport`; no network required.
HTML-to-visible-text conversion uses Python's standard-library `HTMLParser`;
the plugin has no undeclared runtime dependency on Beautiful Soup.

## Status

Version 0.7.1 was import- and regression-tested on 2026-09-26 without `bs4`.
The live production routes previously verified for `wildberries.ru`, `ozon.ru`,
and `avito.ru` continue through the generic Web Unlocker path. See
[`hermes-web-access`](https://github.com/mawlic/hermes-web-access) for the
cross-plugin architecture document, verified-targets table, and known
restrictions.

## Security

The provider checks both the external HTTP status and Bright Data's internal
`x-brd-status-code`, rejects bot-challenge pages, strips scripts/styles from
HTML, and never logs credentials.