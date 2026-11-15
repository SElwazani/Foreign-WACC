# USD vs. Local-Currency WACC Calculator

A Streamlit app that estimates the cost of equity, cost of debt and WACC for a chosen industry in **U.S. dollars** and in the **local currency of a foreign target country**, side by side, using the same project tax rate, debt share and equity risk premium.

## Features

- **Live data with sources and dates** (cached for 6 hours, with a **🔄 Refresh data** button):
  | Input | Source |
  |---|---|
  | U.S. industry unlevered betas | Damodaran – `betas.xls` ("Unlevered beta" column) |
  | Corporate tax rates | OECD – Corporate Tax Statistics 2024, statutory corporate income tax rates (latest year available per country) |
  | USD BBB spread | Damodaran – ratings page, Baa2/BBB, large non-financial service firms |
  | 10-year government yields | Trading Economics – `tradingeconomics.com/bonds` (no API key; scraped) |
- **Controls:** industry, target country, equity risk premium, target debt share.
- **Manual overrides** for unlevered beta, U.S. and target-country tax rates, USD BBB spread, U.S. and target-country 10Y yields, and the foreign BBB spread (switch the override off to see the automatic calculation).
- **Tax-rate switch** between the U.S. rate and the selected country's rate (one project tax rate used in both currencies).
- **Side-by-side results**, an assumptions panel, an **all-industry comparison** (table + chart) and **downloads** (Excel workbook, CSVs).
- **Defaults:** Software (System & Application), Germany as the foreign target country (the U.S. is always the benchmark and cannot be the target), U.S. tax rate, 6.5% ERP, 30% debt.

## Files

```
app.py            # the Streamlit app
requirements.txt  # Python dependencies
README.MD         # this file
```

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push `app.py`, `requirements.txt` and `README.MD` to the root of a GitHub repository.
2. Go to <https://share.streamlit.io>, click **New app**, pick the repository and branch, and set the main file to `app.py`.
3. Click **Deploy**. No secrets or API keys are needed.

## Method

```
D/E       = debt share / (1 − debt share)
β_L       = β_U × [1 + (1 − t) × D/E]
Ke        = risk-free + β_L × ERP
Kd        = risk-free + BBB spread
WACC      = E% × Ke + D% × Kd × (1 − t)
```

- Risk-free rate = 10Y government yield of the respective country (U.S. for USD, selected country for local currency).
- Same unlevered beta, ERP, debt share and project tax rate in both currencies.
- Foreign BBB spread (auto) = USD spread × (1 + local yield) / (1 + U.S. yield), i.e. the same real credit spread expressed in local-currency terms. It can be overridden manually.
- The United States is always the USD benchmark; the target country list contains foreign countries only.

## Notes and limitations

- **Scraping can break.** The OECD, Damodaran and Trading Economics pages are parsed from their HTML/Excel layouts. If a layout changes or a site blocks the request, the app shows which source failed (with the error) and lets you enter that input manually.
- The ratings link supplied for Damodaran has no `~` in the path (`/adamodar/`); the app tries it first and falls back to the usual `/~adamodar/` address. The first Baa2/BBB row found on the page is used – check that it is the large non-financial service firms table.
- Country names are matched between the OECD and Trading Economics (e.g. Türkiye/Turkey, Korea/South Korea). A country missing from either source requires a manual input for that item.
- Local 10Y yields of higher-risk countries include a sovereign default spread, so they are not strictly risk-free rates.
- Statutory (not effective) tax rates are used and full interest deductibility is assumed.
- Educational tool – not investment advice. Verify key inputs against the original sources.
