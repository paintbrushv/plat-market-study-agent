# Comp leasing-engine routing reference

`/pull-comps` (`etl/collect_comps_snapshot.py`) drives every comp through
the engine package at `etl/comp_scraping/`. The dispatcher tries engines in
this order — pick the right `engine:` value per comp in the metro YAML, or
leave it as `auto` and let resolution fall through.

## Engines

| `engine:` value     | URL substring                                     | HTML fingerprint                            | Multi-page capture                              | Notes |
|---------------------|---------------------------------------------------|---------------------------------------------|-------------------------------------------------|-------|
| `cortland`          | `cortland.com/apartments/`                        | `"floorplan_name":"`                        | `/floorplans/`, `/`, `/apply/`                  | SSR Next.js JSON. Per-unit detail extracted. |
| `entrata`           | `entrata.com`, `.entrata.`, `/d2/`                | `"MinRent"`, `Estimated Monthly Cost`       | `/floorplans/` (Playwright), `/`, `/apply/`     | JSON blob in rendered DOM. |
| `rentcafe`          | `rentcafe.com`, `rent-cafe`, `yardi.com`          | `"floorplans":[{`, `rentcafe`               | `/floorplans/`, `/`, `/specials/`               | RentCafe / Yardi-hosted. |
| `jonah_sightmap`    | `thejamesonhighland.com`, `my.hy.ly`, `hyly.us`   | `jd-fp-unit-card`, `sightmap.com/app/api`   | `/floorplans/` (Playwright XHR intercept), DOM, `/` | Hy.ly / Jonah; SightMap XHR captures all units. |
| `swifty`            | `swiftyapp.io`, `swifty.com`                      | `single-floorplan`, `swifty-app`            | `/floorplans/`, `/`                             | |
| `h2`                | `h2realestate`, `h2-realestate`                   | `/ MONTH`                                   | `/`                                             | H2 listings layout. |
| `appfolio_listings` | `appfolio.com/listings`, `appfoliopm`             | `Appfolio.Listing`, `listing-item`          | base URL                                        | Engine activates when `appfolio_listings_url` is set on the comp. |
| `resi_rendered`     | `myashwoodpark.com`                               | `uk-tile uk-padding-small`                  | `/floorplans/` (Playwright), `/`                | UIKit cards on Vue. |
| `llm_fallback`      | (no URL match)                                    | (no fingerprint match)                      | `/floorplans/` (Playwright), `/`                | Anthropic API extraction. Set `ANTHROPIC_API_KEY`; opt-in to Opus 4.7 with `MARKET_STUDY_LLM_QUALITY=1`. |

## YAML contract per comp

Under `comp_monitoring.comps` in any metro config, each comp accepts:

```yaml
- name: "Comp Name"
  address: "1234 Main St, City, ST 00000"
  units: 273                              # Total unit count → drives quality-gate threshold
  apartments_com_url: "https://www.apartments.com/.../"
  property_url: "https://www.example.com/" # Property's actual website
  direct_floorplans_url: "https://www.example.com/floorplans"  # Canonical engine URL
  engine: rentcafe                        # Optional hint; `auto` lets the dispatcher pick
  appfolio_listings_url: "..."            # Optional; takes priority when set
  extra_capture_urls:                      # Optional override list of additional pages
    - "https://www.example.com/specials"
```

`units` is also looked up from `leaseup_tracking.properties[*].units` if not
declared on the comp itself.

## Probe a metro

To audit which engine each comp resolves to (and discover floorplan / specials
URLs), run:

```bash
uv run python etl/probe_comp_engines.py --config agents/configs/<metro>.yaml
```

The script writes a YAML patch to stdout — review and merge into the metro
config under `comp_monitoring.comps[*]`. Confidence levels in the patch:

| Confidence | Meaning |
|------------|---------|
| `high`     | URL substring match — engine is certain |
| `medium`   | HTML fingerprint match — re-verify after a render-engine change |
| `low`      | LLM fallback only; engine cascade left no signal |

## Quality gate

`etl.comp_scraping.quality_gate.passes()` fails any payload with fewer than
`max(1, units // 20)` priced floorplans. A failed gate triggers Tier-3 LLM
extraction (when `ANTHROPIC_API_KEY` is set).

## Adding a new engine

1. Create `etl/comp_scraping/engines/<engine>.py` modelled on `cortland.py`
   (or `_base.SimpleEngine` if a thin wrapper is enough).
2. Add the new module to `BUILTIN_ENGINES` in `etl/comp_scraping/engines/__init__.py`
   — order matters when URLs overlap (specific operators before generic platforms).
3. Add a row to the table above.
4. Add a fixture under `tests/fixtures/<engine>_floorplans.html` and a parser
   test under `tests/test_comp_scraping/test_engines.py`.
