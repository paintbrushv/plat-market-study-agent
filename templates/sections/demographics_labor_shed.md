### Demographic & Labor Shed Analysis

#### Catchment Area Definition

- **Subject**: {{property_name}} at {{address}}
- **Method**: Drive-time isochrones (OSRM routing), not radius rings
- **Analysis rings**: {{drive_time_rings}} minutes
- **Data**: ACS {{acs_year}} 5-Year Estimates, LODES {{lodes_year}}, BLS QCEW {{qcew_year}}

#### Population & Housing

| Metric | {{ring_1}}-min | {{ring_2}}-min | {{ring_3}}-min | County | Source |
| --- | ---: | ---: | ---: | ---: | --- |
| Population | {{pop_r1}} | {{pop_r2}} | {{pop_r3}} | {{pop_county}} | [CENSUS-ACS, {{acs_year}}] |
| Median Age | {{age_r1}} | {{age_r2}} | {{age_r3}} | {{age_county}} | [CENSUS-ACS, {{acs_year}}] |
| Median HH Income | {{income_r1}} | {{income_r2}} | {{income_r3}} | {{income_county}} | [CENSUS-ACS, {{acs_year}}] |
| Housing Units | {{units_r1}} | {{units_r2}} | {{units_r3}} | {{units_county}} | [CENSUS-ACS, {{acs_year}}] |
| Vacancy Rate | {{vac_r1}} | {{vac_r2}} | {{vac_r3}} | {{vac_county}} | [CENSUS-ACS, {{acs_year}}] |
| Renter Pct | {{renter_r1}} | {{renter_r2}} | {{renter_r3}} | {{renter_county}} | [CENSUS-ACS, {{acs_year}}] |
| Median Rent | {{rent_r1}} | {{rent_r2}} | {{rent_r3}} | {{rent_county}} | [CENSUS-ACS, {{acs_year}}] |

#### Employment Profile ({{primary_ring}}-min drivetime)

| Sector | Jobs | Pct | Avg Annual Pay | Source |
| --- | ---: | ---: | ---: | --- |
{{#each top_sectors}}
| {{naics_title}} | {{annual_avg_employment}} | {{pct_of_total}} | {{avg_annual_pay}} | [LODES {{lodes_year}}, QCEW {{qcew_year}}] |
{{/each}}
| **Total** | **{{total_jobs}}** | **100%** | **{{weighted_avg_pay}}** | |

#### Wage Tier Distribution (LODES)

| Tier | Jobs | Pct | Implied Annual Range |
| --- | ---: | ---: | --- |
| Low (≤$1,250/mo) | {{earn_low}} | {{earn_low_pct}} | ≤$15,000 |
| Mid ($1,251–$3,333/mo) | {{earn_mid}} | {{earn_mid_pct}} | $15,001–$40,000 |
| High (>$3,333/mo) | {{earn_high}} | {{earn_high_pct}} | >$40,000 |

#### Labor Shed — Where Workers Commute From

Workers employed within the {{primary_ring}}-minute drive-time catchment reside in these counties:

| Home County | Workers | Pct | Cumulative |
| --- | ---: | ---: | ---: |
{{#each commute_counties}}
| {{county_name}} | {{total_jobs}} | {{pct_of_total}} | {{cumulative_pct}} |
{{/each}}

> Top {{top_n_counties}} counties account for {{top_n_pct}}% of the workforce.
> Data: [LEHD LODES OD, {{lodes_year}}]

#### Workforce-Housing Affordability

| Metric | Value |
| --- | ---: |
| Subject Avg Rent | {{avg_rent}}/mo |
| Annual Rent Burden | {{annual_rent}}/yr |
| Required Income (30% rule) | {{required_income}}/yr |
| Catchment Median HH Income | {{median_income}}/yr |
| Affordability Ratio | {{affordability_ratio}} |
| Median HH Can Afford | {{affordability_verdict}} |

{{#if affordability_note}}
> {{affordability_note}}
{{/if}}

#### Demographic Trajectory ({{trend_start}}–{{trend_end}})

| Metric | {{trend_start}} | {{trend_end}} | CAGR | Source |
| --- | ---: | ---: | ---: | --- |
| Population | {{pop_start}} | {{pop_end}} | {{pop_cagr}} | [CENSUS-ACS] |
| Median HH Income | {{income_start}} | {{income_end}} | {{income_cagr}} | [CENSUS-ACS] |
| Renter-Occupied Units | {{renter_start}} | {{renter_end}} | {{renter_cagr}} | [CENSUS-ACS] |
| Median Rent | {{mrent_start}} | {{mrent_end}} | {{mrent_cagr}} | [CENSUS-ACS] |
| Housing Units | {{hunits_start}} | {{hunits_end}} | {{hunits_cagr}} | [CENSUS-ACS] |
