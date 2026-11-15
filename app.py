USD vs. local-currency WACC calculator (Streamlit)

Data pulled live (with caching + refresh button):
  * Damodaran  - U.S. industry unlevered betas (betas.xls)
  * OECD       - statutory corporate income tax rates (Corporate Tax Statistics)
  * Damodaran  - Baa2/BBB default spread, large non-financial service firms (ratings.html)
  * Trading Economics - 10-year government bond yields (tradingeconomics.com/bonds)

Every fetched number can be overridden manually in the sidebar.
"""

import io
import re
import unicodedata
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
BETA_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
OECD_URL = (
    "https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/"
    "full-report/statutory-corporate-income-tax-rates_440f9286.html"
)
# The link supplied for the ratings page has no "~"; Damodaran's site normally uses
# "/~adamodar/". Both are tried (in this order).
RATINGS_URLS = [
    "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html",
    "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.html",
]
TE_URLS = [
    "https://tradingeconomics.com/bonds",
    "https://tradingeconomics.com/country-list/government-bond-yield",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
CACHE_TTL = 6 * 3600  # seconds; the refresh button clears the cache immediately

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY_KEY = "united states"
DEFAULT_ERP_PCT = 6.5
DEFAULT_DEBT_PCT = 30

MONTH_RE = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(?:\d{1,2},\s*)?\d{4}"
)

# Country-name harmonisation between OECD and Trading Economics (keys are lower-case)
ALIASES = {
    "turkiye": "turkey",
    "korea": "south korea",
    "republic of korea": "south korea",
    "korea, republic of": "south korea",
    "slovak republic": "slovakia",
    "czechia": "czech republic",
    "united states of america": "united states",
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "uk": "united kingdom",
    "russian federation": "russia",
    "people's republic of china": "china",
    "china (people's republic of)": "china",
    "hong kong, china": "hong kong",
    "hong kong (china)": "hong kong",
    "hong kong sar, china": "hong kong",
    "viet nam": "vietnam",
    "cote d'ivoire": "ivory coast",
    "brunei darussalam": "brunei",
    "macao (china)": "macau",
    "north macedonia": "macedonia",
    "eswatini": "swaziland",
    "republic of ireland": "ireland",
}


# ----------------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def http_get(url, timeout=40):
    r = requests.get(url.split("#")[0], headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def clean_display(name):
    s = re.sub(r"\b10\s?Y\b", "", str(name), flags=re.I)
    s = re.sub(r"[\*†‡]+", "", s)
    s = re.sub(r"\s*\d+$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_country(name):
    s = _strip_accents(clean_display(name)).lower()
    if s in ALIASES:
        return ALIASES[s]
    s2 = re.sub(r"\s*\(.*?\)", "", s).strip()
    return ALIASES.get(s2, s2)


def to_num(x):
    """First number found in a cell (handles '4.12%', '25.8a', NaN) or None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return None if pd.isna(x) else float(x)
    m = re.search(r"-?\d+(?:\.\d+)?", str(x).replace("−", "-").replace(",", ""))
    return float(m.group(0)) if m else None


def find_date(text, keywords=("last updated", "date of analysis", "as of", "updated")):
    t = re.sub(r"\s+", " ", text or "")
    low = t.lower()
    for kw in keywords:
        idx = low.find(kw)
        if idx >= 0:
            m = re.search(MONTH_RE, t[idx: idx + 160])
            if m:
                return m.group(0)
    m = re.search(MONTH_RE, t)
    return m.group(0) if m else None


def _result(url):
    return {"ok": False, "error": "", "fetched_at": _now(), "url": url, "as_of": "n/a"}


# ----------------------------------------------------------------------------
# Parsers (kept separate from the network code so they are easy to test)
# ----------------------------------------------------------------------------
def locate_header(df, token, max_rows=40):
    for i in range(min(max_rows, len(df))):
        vals = [str(v).strip().lower() for v in df.iloc[i].tolist()]
        if token in vals:
            return i
    return None


def with_header_row(t):
    """pandas moves <th> cells into the column labels; put them back as the first data row."""
    cols = [
        " ".join(str(x) for x in c if "Unnamed" not in str(x)).strip()
        if isinstance(c, tuple) else str(c)
        for c in t.columns
    ]
    body = t.copy()
    body.columns = range(len(cols))
    return pd.concat([pd.DataFrame([cols]), body], ignore_index=True)


def find_year_header(t):
    for i in range(min(8, len(t))):
        yrs = {}
        for j, v in enumerate(t.iloc[i].tolist()):
            m = re.fullmatch(r"((?:19|20)\d{2})(?:\.0)?", str(v).strip())
            if m:
                yrs[j] = int(m.group(1))
        if len(yrs) >= 5:
            return i, yrs
    return None


def parse_oecd_tables(tables):
    """Return {country_key: {country, rate (decimal), year}} using the latest year per country."""
    best = {}
    for t in tables:
        t = with_header_row(t)
        found = find_year_header(t)
        if not found:
            continue
        hi, yrs = found
        rows = {}
        for r in range(hi + 1, len(t)):
            name = t.iat[r, 0]
            if pd.isna(name):
                continue
            disp = clean_display(name)
            if not re.search(r"[A-Za-z]{3,}", disp):
                continue
            for j, yr in sorted(yrs.items(), key=lambda kv: -kv[1]):
                v = to_num(t.iat[r, j])
                if v is not None and 0 <= v <= 100:
                    rows[norm_country(name)] = {"country": disp, "rate_raw": v, "year": yr}
                    break
        if len(rows) > len(best):
            best = rows
    if best:
        scale = 1.0 if max(x["rate_raw"] for x in best.values()) <= 1.0 else 100.0
        for x in best.values():
            x["rate"] = x.pop("rate_raw") / scale
    return best


def parse_te_tables(tables):
    """Return {country_key: {country, yield (decimal), date}} from Trading Economics tables."""
    rows = {}
    for t in tables:
        t = t.copy()
        if isinstance(t.columns, pd.MultiIndex):
            t.columns = [
                " ".join(str(x) for x in col if "Unnamed" not in str(x)).strip()
                for col in t.columns
            ]
        t.columns = [str(c) for c in t.columns]
        t = t.loc[:, ~t.columns.duplicated()]
        lower = {c: c.lower() for c in t.columns}
        ycol = next((c for c, l in lower.items() if "yield" in l), None) or next(
            (c for c, l in lower.items() if l == "last"), None
        )
        if ycol is None:
            continue
        dcol = next((c for c, l in lower.items() if "date" in l or "reference" in l), None)
        ccol = None
        for c in t.columns:
            if c in (ycol, dcol):
                continue
            s = t[c].dropna().astype(str)
            if len(s) and s.str.contains(r"[A-Za-z]{3,}").mean() > 0.6:
                ccol = c
                break
        if ccol is None:
            continue
        for _, row in t.iterrows():
            name = row[ccol]
            y = to_num(row[ycol])
            if pd.isna(name) or y is None or not (-10 < y < 100):
                continue
            key = norm_country(name)
            if not key or key in rows:
                continue
            d = ""
            if dcol is not None and not pd.isna(row[dcol]):
                d = str(row[dcol]).strip()
            rows[key] = {"country": clean_display(name), "yield": y / 100.0, "date": d}
    return rows


def parse_spread_tables(tables):
    """Find the Baa2/BBB row and return its spread as a decimal (last numeric cell)."""
    for t in tables:
        t = with_header_row(t).astype(object)
        for i in range(len(t)):
            cells = t.iloc[i].tolist()
            if not any(
                isinstance(c, str) and "baa2/bbb" in c.lower().replace(" ", "") for c in cells
            ):
                continue
            val = None
            for c in cells:
                if isinstance(c, str) and "baa2" in c.lower():
                    continue
                v = to_num(c)
                if v is None:
                    continue
                pct = isinstance(c, str) and "%" in c
                val = v / 100.0 if (pct or v >= 0.5) else v
            if val is not None and 0.001 <= val <= 0.10:
                return val
    return None


# ----------------------------------------------------------------------------
# Fetchers (cached)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_betas():
    res = _result(BETA_URL)
    res["df"] = pd.DataFrame()
    try:
        r = http_get(BETA_URL, timeout=60)
        sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None, header=None)
        chosen = None
        for name, sh in sheets.items():
            if "industry" in name.lower() and "average" in name.lower():
                h = locate_header(sh, "industry name")
                if h is not None:
                    chosen = (name, sh, h)
                    break
        if chosen is None:
            for name, sh in sheets.items():
                h = locate_header(sh, "industry name")
                if h is not None:
                    chosen = (name, sh, h)
                    break
        if chosen is None:
            raise ValueError("Could not find the 'Industry Name' header in betas.xls")
        _, sh, h = chosen
        cols = [str(c).strip() for c in sh.iloc[h].tolist()]
        body = sh.iloc[h + 1:].copy()
        body.columns = cols
        body = body.loc[:, ~body.columns.duplicated()]

        def col(pred):
            return next((c for c in cols if pred(c.lower())), None)

        c_name = col(lambda c: c == "industry name")
        c_n = col(lambda c: c.startswith("number of firms"))
        c_de = col(lambda c: c.startswith("d/e"))
        c_ub = col(lambda c: c == "unlevered beta") or col(
            lambda c: "unlevered" in c and "cash" not in c
        )
        if c_ub is None:
            raise ValueError("Could not find the 'Unlevered beta' column")

        df = pd.DataFrame(
            {
                "Industry": body[c_name].astype(str).str.strip(),
                "Firms": pd.to_numeric(body[c_n], errors="coerce") if c_n else float("nan"),
                "Avg D/E (%)": pd.to_numeric(body[c_de], errors="coerce") * 100 if c_de else float("nan"),
                "Unlevered beta": pd.to_numeric(body[c_ub], errors="coerce"),
            }
        )
        low = df["Industry"].str.lower()
        df = df[
            df["Unlevered beta"].notna()
            & ~low.isin(["", "nan"])
            & ~low.str.startswith("total market")
        ].drop_duplicates("Industry")
        if df.empty:
            raise ValueError("No industry rows parsed from betas.xls")

        head_text = " ".join(str(v) for v in sh.iloc[:h].fillna("").values.flatten())
        as_of = find_date(head_text, ("date of analysis", "as of", "updated"))
        if not as_of and r.headers.get("Last-Modified"):
            as_of = f"file last modified {r.headers['Last-Modified']}"
        res.update(ok=True, df=df.reset_index(drop=True), as_of=as_of or "not stated in file")
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_oecd_tax():
    res = _result(OECD_URL)
    res["rows"] = {}
    try:
        r = http_get(OECD_URL)
        tables = pd.read_html(io.StringIO(r.text), header=None)
        rows = parse_oecd_tables(tables)
        if len(rows) < 10:
            raise ValueError("Could not find the statutory tax-rate table on the OECD page")
        years = [x["year"] for x in rows.values()]
        res.update(
            ok=True,
            rows=rows,
            as_of=f"latest year available per country (most recent column: {max(years)})",
        )
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_spread():
    res = _result(RATINGS_URLS[0])
    res["spread"] = None
    errors = []
    for url in RATINGS_URLS:
        try:
            r = http_get(url)
            tables = pd.read_html(io.StringIO(r.text), header=None)
            sp = parse_spread_tables(tables)
            if sp is None:
                raise ValueError("Baa2/BBB row not found")
            text = BeautifulSoup(r.text, "html.parser").get_text(" ")
            res.update(
                ok=True,
                spread=sp,
                url=url,
                as_of=find_date(text, ("last updated", "date of analysis", "updated")) or "not stated on page",
                error="",
            )
            return res
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {e}")
    res["error"] = " | ".join(errors)
    return res


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_yields():
    res = _result(TE_URLS[0])
    res["rows"] = {}
    errors = []
    for url in TE_URLS:
        try:
            r = http_get(url)
            tables = pd.read_html(io.StringIO(r.text))
            rows = parse_te_tables(tables)
            if len(rows) < 10:
                raise ValueError("Yield table not found / too few rows")
            res.update(ok=True, rows=rows, url=url, as_of="see per-country date", error="")
            return res
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {e}")
    res["error"] = " | ".join(errors)
    return res


# ----------------------------------------------------------------------------
# Finance
# ----------------------------------------------------------------------------
def compute(beta_u, tax, erp, debt_w, rf, spread):
    """CAPM cost of equity, rf+spread cost of debt, WACC (single currency)."""
    de = debt_w / (1.0 - debt_w)
    beta_l = beta_u * (1.0 + (1.0 - tax) * de)
    ke = rf + beta_l * erp
    kd = rf + spread
    kd_at = kd * (1.0 - tax)
    wacc = (1.0 - debt_w) * ke + debt_w * kd_at
    return {
        "rf": rf, "beta_u": beta_u, "de": de, "beta_l": beta_l, "erp": erp, "ke": ke,
        "spread": spread, "kd": kd, "tax": tax, "kd_at": kd_at,
        "we": 1.0 - debt_w, "wd": debt_w, "wacc": wacc,
    }


def auto_foreign_spread(sp_us, rf_us, rf_f):
    """Same real credit spread: USD spread scaled by the ratio of (1 + risk-free) rates."""
    return sp_us * (1.0 + rf_f) / (1.0 + rf_us)


# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------
def override_input(label, key, fetched, fallback, *, pct=True, lo=0.0, hi=100.0,
                   step=0.05, fmt="%.3f", toggle_label=None, missing_msg=None):
    """Fetched value (disabled box) or a manual override. Returns (value, is_manual)."""
    unit = " (%)" if pct else ""
    if fetched is None:
        st.warning(missing_msg or f"No fetched value for {label.lower()} - please enter it manually.")
        manual, base = True, fallback
    else:
        base = round(fetched * 100, 4) if pct else round(fetched, 4)
        manual = st.checkbox(toggle_label or f"Override {label.lower()}", key=f"{key}_on")
    shown = float(min(max(base, lo), hi))
    if manual:
        v = st.number_input(label + unit, min_value=lo, max_value=hi, value=shown,
                            step=step, format=fmt, key=f"{key}_val")
    else:
        st.number_input(label + unit + " - fetched", min_value=lo, max_value=hi, value=shown,
                        step=step, format=fmt, disabled=True, key=f"{key}_auto_{base}")
        v = base
    return (v / 100.0 if pct else v), manual


def show_chart(chart):
    try:
        st.altair_chart(chart, width="stretch")
    except TypeError:
        st.altair_chart(chart, use_container_width=True)


def fmt_pct(x):
    return f"{x * 100:.2f}%"


def make_excel(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
    return buf.getvalue()


RESULT_ROWS = [
    ("Risk-free rate (10Y government yield)", "rf", "pct"),
    ("Unlevered beta", "beta_u", "num"),
    ("Target debt / equity", "de", "num"),
    ("Levered beta", "beta_l", "num"),
    ("Equity risk premium", "erp", "pct"),
    ("Cost of equity", "ke", "pct"),
    ("BBB credit spread", "spread", "pct"),
    ("Pre-tax cost of debt", "kd", "pct"),
    ("Project tax rate", "tax", "pct"),
    ("After-tax cost of debt", "kd_at", "pct"),
    ("Equity weight", "we", "pct"),
    ("Debt weight", "wd", "pct"),
    ("WACC", "wacc", "pct"),
]


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="USD vs Local-Currency WACC", page_icon="📊", layout="wide")
    st.title("📊 WACC in USD vs. local currency")
    st.caption(
        "Industry betas and the BBB spread from Damodaran, statutory tax rates from the OECD, "
        "10-year government yields from Trading Economics. Every input can be overridden."
    )

    with st.spinner("Fetching the latest data (Damodaran, OECD, Trading Economics)..."):
        betas = fetch_betas()
        oecd = fetch_oecd_tax()
        spread = fetch_spread()
        yields = fetch_yields()

    tax_rows = oecd.get("rows", {})
    yld_rows = yields.get("rows", {})

    # Country universe (union of OECD and Trading Economics names)
    names = {}
    for src, field in ((yld_rows, "country"), (tax_rows, "country")):
        for k, v in src.items():
            names.setdefault(k, v[field])
    names.setdefault(DEFAULT_COUNTRY_KEY, "United States")
    country_keys = sorted(names, key=lambda k: names[k].lower())

    # ------------------------------------------------------------------ sidebar
    with st.sidebar:
        st.header("⚙️ Controls")
        if st.button("🔄 Refresh data", help="Clear the cache and fetch every source again"):
            for f in (fetch_betas, fetch_oecd_tax, fetch_spread, fetch_yields):
                f.clear()
            st.rerun()
        st.caption(f"Data fetched: {max(x['fetched_at'] for x in (betas, oecd, spread, yields))}")

        # Selections
        industries = sorted(betas["df"]["Industry"].tolist()) if betas["ok"] else []
        if industries:
            idx = next((i for i, n in enumerate(industries) if n == DEFAULT_INDUSTRY), None)
            if idx is None:
                idx = next((i for i, n in enumerate(industries)
                            if n.lower().startswith("software (system")), 0)
            industry = st.selectbox("Industry", industries, index=idx)
        else:
            industry = "Manual industry (beta data unavailable)"
            st.error("Industry betas could not be fetched - enter the beta manually below.")

        ckey = st.selectbox(
            "Target country", country_keys,
            index=country_keys.index(DEFAULT_COUNTRY_KEY),
            format_func=lambda k: names[k],
        )
        country = names[ckey]
        is_us = ckey == DEFAULT_COUNTRY_KEY

        erp = st.number_input("Equity risk premium (%)", 0.0, 25.0, DEFAULT_ERP_PCT,
                              step=0.1, format="%.2f") / 100.0
        debt_w = st.slider("Target debt share of capital (%)", 0, 95, DEFAULT_DEBT_PCT) / 100.0

        # Fetched values
        beta_f = None
        if industries:
            beta_f = float(betas["df"].loc[betas["df"]["Industry"] == industry, "Unlevered beta"].iloc[0])
        tax_us_f = tax_rows.get(DEFAULT_COUNTRY_KEY, {}).get("rate")
        tax_for_f = tax_rows.get(ckey, {}).get("rate")
        sp_us_f = spread.get("spread")
        rf_us_f = yld_rows.get(DEFAULT_COUNTRY_KEY, {}).get("yield")
        rf_for_f = yld_rows.get(ckey, {}).get("yield")

        # Overrides
        st.subheader("✏️ Manual overrides")
        st.caption("Tick a box to replace the fetched value.")
        beta_u, beta_man = override_input(
            "Unlevered beta", "beta", beta_f, 1.0, pct=False, lo=0.0, hi=10.0, step=0.01)
        sp_us, sp_us_man = override_input(
            "USD BBB spread", "sp_us", sp_us_f, 1.5, lo=0.0, hi=50.0, step=0.05)
        rf_us, rf_us_man = override_input(
            "U.S. 10Y government yield", "rf_us", rf_us_f, 4.0, lo=-10.0, hi=100.0, step=0.05)
        tax_us, tax_us_man = override_input(
            "U.S. corporate tax rate", "tax_us", tax_us_f, 25.0, lo=0.0, hi=100.0, step=0.1)

        if is_us:
            st.info("Target country = United States, so the 'foreign' inputs mirror the U.S. inputs.")
            rf_f, rf_f_man = rf_us, rf_us_man
            tax_f, tax_f_man = tax_us, tax_us_man
            sp_f, sp_f_man = sp_us, sp_us_man
        else:
            rf_f, rf_f_man = override_input(
                f"{country} 10Y government yield", "rf_f", rf_for_f, 4.0,
                lo=-10.0, hi=100.0, step=0.05)
            tax_f, tax_f_man = override_input(
                f"{country} corporate tax rate", "tax_f", tax_for_f, 25.0,
                lo=0.0, hi=100.0, step=0.1)
            sp_f, sp_f_man = override_input(
                f"{country} BBB spread", "sp_f", auto_foreign_spread(sp_us, rf_us, rf_f), 1.5,
                lo=0.0, hi=100.0, step=0.05,
                toggle_label="Override foreign BBB spread (off = auto calculation)")

    # ------------------------------------------------------------------ tax switch
    if is_us:
        tax, tax_label = tax_us, "United States"
        st.info("Project tax rate: U.S. rate (the selected country is the United States).")
    else:
        choice = st.radio(
            "Project tax rate (used in both currencies)",
            ["us", "for"],
            format_func=lambda c: (
                f"U.S. rate ({fmt_pct(tax_us)})" if c == "us" else f"{country} rate ({fmt_pct(tax_f)})"
            ),
            horizontal=True,
            key="tax_choice",
        )
        tax, tax_label = (tax_us, "United States") if choice == "us" else (tax_f, country)

    # ------------------------------------------------------------------ results
    r_us = compute(beta_u, tax, erp, debt_w, rf_us, sp_us)
    r_f = compute(beta_u, tax, erp, debt_w, rf_f, sp_f)
    local_label = f"{country} (local currency)" if not is_us else "United States (same as USD)"

    st.subheader(f"Results - {industry}")
    c1, c2 = st.columns(2)
    for col, title, r, ref in ((c1, "🇺🇸 USD", r_us, None), (c2, f"🌍 {local_label}", r_f, r_us)):
        with col:
            st.markdown(f"**{title}**")
            m1, m2, m3 = st.columns(3)
            m1.metric("Cost of equity", fmt_pct(r["ke"]))
            m2.metric("After-tax cost of debt", fmt_pct(r["kd_at"]))
            m3.metric(
                "WACC", fmt_pct(r["wacc"]),
                delta=None if ref is None else f"{(r['wacc'] - ref['wacc']) * 100:+.2f} pp vs USD",
                delta_color="off",
            )

    table = pd.DataFrame(
        {
            "Metric": [m for m, _, _ in RESULT_ROWS],
            "USD": [fmt_pct(r_us[k]) if t == "pct" else f"{r_us[k]:.3f}" for _, k, t in RESULT_ROWS],
            local_label: [fmt_pct(r_f[k]) if t == "pct" else f"{r_f[k]:.3f}" for _, k, t in RESULT_ROWS],
        }
    ).set_index("Metric")
    st.table(table)

    # ------------------------------------------------------------------ assumptions
    beta_note = " - manual" if beta_man else ""
    sp_us_note = " - manual" if sp_us_man else ""
    sp_f_note = " - manual" if (sp_f_man and not is_us) else ""
    if is_us:
        sp_f_desc = "the same as the USD spread (target country is the U.S.)"
    elif sp_f_man:
        sp_f_desc = "manual override"
    else:
        sp_f_desc = (
            "auto: USD spread × (1 + local yield) / (1 + U.S. yield), "
            "i.e. the same real credit spread expressed in local-currency terms"
        )
    assumptions = "\n".join(
        [
            "- **Beta:** Damodaran's U.S. industry *unlevered* beta ("
            + f"{beta_u:.3f}{beta_note}"
            + ") is re-levered at the target capital structure: "
            + "`β_L = β_U × [1 + (1 − t) × D/E]`, with `D/E = debt share / (1 − debt share)` = "
            + f"{r_us['de']:.3f}. The same beta is used in both currencies.",
            "- **Cost of equity (CAPM):** `Ke = risk-free + β_L × ERP`. The risk-free rate is the "
            + "10-year government yield of the respective country "
            + f"(U.S. {fmt_pct(rf_us)}, {country} {fmt_pct(rf_f)}); the **same ERP ({fmt_pct(erp)})** "
            + "is applied in both currencies, so the gap between the two currencies comes only from "
            + "the risk-free rates and the credit spreads. For high-risk sovereigns the local yield "
            + "also contains a sovereign default spread.",
            "- **Cost of debt:** `Kd = risk-free + BBB spread`. USD spread = Damodaran's Baa2/BBB "
            + "spread for large non-financial service firms "
            + f"({fmt_pct(sp_us)}{sp_us_note}). Foreign spread ({fmt_pct(sp_f)}{sp_f_note}) = "
            + sp_f_desc
            + ".",
            "- **Taxes:** one project tax rate for both currencies - "
            + f"**{tax_label}: {fmt_pct(tax)}** "
            + "(OECD combined central + sub-central statutory rate unless overridden). "
            + "It is used in the re-levering formula and the interest tax shield "
            + "(full deductibility is assumed).",
            "- **WACC:** `WACC = E% × Ke + D% × Kd × (1 − t)` with a target debt share of "
            + f"{fmt_pct(debt_w)} and the same weights in both currencies.",
            "- Simplifications: industry betas are U.S.-based averages; no country-specific equity "
            + "risk adjustment; statutory (not effective/marginal) tax rates.",
        ]
    )
    with st.expander("📘 Main assumptions & formulas", expanded=True):
        st.markdown(assumptions)

    # ------------------------------------------------------------------ all-industry comparison
    st.subheader("All-industry comparison")
    comp = pd.DataFrame()
    if betas["ok"]:
        rows = []
        for _, r in betas["df"].iterrows():
            bu = beta_u if (beta_man and r["Industry"] == industry) else r["Unlevered beta"]
            a = compute(bu, tax, erp, debt_w, rf_us, sp_us)
            b = compute(bu, tax, erp, debt_w, rf_f, sp_f)
            rows.append(
                {
                    "Industry": r["Industry"],
                    "Selected": "★" if r["Industry"] == industry else "",
                    "Firms": r["Firms"],
                    "Unlevered beta": bu,
                    "Levered beta": a["beta_l"],
                    "Ke USD (%)": a["ke"] * 100,
                    "Ke local (%)": b["ke"] * 100,
                    "WACC USD (%)": a["wacc"] * 100,
                    "WACC local (%)": b["wacc"] * 100,
                    "Local - USD (pp)": (b["wacc"] - a["wacc"]) * 100,
                }
            )
        comp = pd.DataFrame(rows).sort_values("WACC USD (%)").reset_index(drop=True)
        st.caption(
            "Uses the sidebar settings (tax rate, ERP, debt share, yields, spreads) for every industry. "
            "A manual beta override applies only to the selected industry."
        )
        st.dataframe(
            comp, hide_index=True,
            column_config={
                "Firms": st.column_config.NumberColumn(format="%d"),
                "Unlevered beta": st.column_config.NumberColumn(format="%.3f"),
                "Levered beta": st.column_config.NumberColumn(format="%.3f"),
                "Ke USD (%)": st.column_config.NumberColumn(format="%.2f"),
                "Ke local (%)": st.column_config.NumberColumn(format="%.2f"),
                "WACC USD (%)": st.column_config.NumberColumn(format="%.2f"),
                "WACC local (%)": st.column_config.NumberColumn(format="%.2f"),
                "Local - USD (pp)": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        long = comp.melt(
            id_vars=["Industry"], value_vars=["WACC USD (%)", "WACC local (%)"],
            var_name="Currency", value_name="WACC_pct",
        )
        long["Currency"] = long["Currency"].map(
            {"WACC USD (%)": "USD", "WACC local (%)": local_label})
        chart = (
            alt.Chart(long)
            .mark_circle(size=55)
            .encode(
                x=alt.X("Industry:N", sort=comp["Industry"].tolist(),
                        axis=alt.Axis(labelAngle=-90, labelLimit=240, title=None)),
                y=alt.Y("WACC_pct:Q", title="WACC (%)", scale=alt.Scale(zero=False)),
                color=alt.Color("Currency:N", title=None),
                tooltip=["Industry", "Currency", alt.Tooltip("WACC_pct:Q", format=".2f")],
            )
            .properties(height=420, width="container")
        )
        show_chart(chart)
    else:
        st.info("Industry betas are unavailable, so the all-industry comparison cannot be built. "
                "Refresh the data or check the sources below.")

    # ------------------------------------------------------------------ sources table
    def origin(manual):
        return "Manual" if manual else "Fetched"

    te_us_date = yld_rows.get(DEFAULT_COUNTRY_KEY, {}).get("date", "") or "n/a"
    te_for_date = yld_rows.get(ckey, {}).get("date", "") or "n/a"
    oecd_us_year = tax_rows.get(DEFAULT_COUNTRY_KEY, {}).get("year", "n/a")
    oecd_for_year = tax_rows.get(ckey, {}).get("year", "n/a")
    src = [
        [f"Unlevered beta - {industry}", f"{beta_u:.3f}", origin(beta_man),
         "Damodaran - US industry betas", betas["as_of"], BETA_URL],
        ["U.S. corporate tax rate", fmt_pct(tax_us), origin(tax_us_man),
         "OECD Corporate Tax Statistics", f"year {oecd_us_year}", OECD_URL],
        ["USD BBB spread", fmt_pct(sp_us), origin(sp_us_man),
         "Damodaran - ratings (Baa2/BBB, large non-financial firms)", spread["as_of"], spread["url"]],
        ["U.S. 10Y government yield", fmt_pct(rf_us), origin(rf_us_man),
         "Trading Economics", f"{te_us_date} (fetched {yields['fetched_at']})", yields["url"]],
    ]
    if not is_us:
        src += [
            [f"{country} corporate tax rate", fmt_pct(tax_f), origin(tax_f_man),
             "OECD Corporate Tax Statistics", f"year {oecd_for_year}", OECD_URL],
            [f"{country} 10Y government yield", fmt_pct(rf_f), origin(rf_f_man),
             "Trading Economics", f"{te_for_date} (fetched {yields['fetched_at']})", yields["url"]],
            [f"{country} BBB spread", fmt_pct(sp_f), "Manual" if sp_f_man else "Auto-calculated",
             "USD spread × (1 + local yield) / (1 + U.S. yield)", "derived", "-"],
        ]
    src_df = pd.DataFrame(src, columns=["Item", "Value used", "Origin", "Source", "Data date", "URL"])

    st.subheader("Data sources & dates")
    st.dataframe(src_df, hide_index=True, column_config={"URL": st.column_config.LinkColumn("URL")})
    for label, res in (
        ("Damodaran industry betas", betas), ("OECD corporate tax rates", oecd),
        ("Damodaran BBB spread", spread), ("Trading Economics yields", yields),
    ):
        if res["ok"]:
            st.markdown(f"✅ **{label}** - fetched {res['fetched_at']}")
        else:
            st.markdown(f"⚠️ **{label}** - fetch failed; use manual inputs.")
            with st.expander(f"Error details - {label}"):
                st.code(res["error"] or "Unknown error")

    # ------------------------------------------------------------------ downloads
    st.subheader("⬇️ Download results")
    summary = pd.DataFrame(
        {
            "Metric": [m + (" (%)" if t == "pct" else "") for m, _, t in RESULT_ROWS],
            "USD": [r_us[k] * (100 if t == "pct" else 1) for _, k, t in RESULT_ROWS],
            local_label: [r_f[k] * (100 if t == "pct" else 1) for _, k, t in RESULT_ROWS],
        }
    )
    settings = pd.DataFrame(
        {
            "Setting": ["Industry", "Target country", "Project tax rate basis", "ERP (%)",
                        "Debt share (%)", "Exported (UTC)"],
            "Value": [industry, country, tax_label, erp * 100, debt_w * 100, _now()],
        }
    )
    d1, d2, d3 = st.columns(3)
    d1.download_button(
        "Excel workbook (all sheets)",
        make_excel({"Summary": summary, "Settings": settings,
                    "All industries": comp if not comp.empty else pd.DataFrame(),
                    "Sources": src_df}),
        file_name="wacc_results.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    d2.download_button("Side-by-side results (CSV)", summary.to_csv(index=False).encode("utf-8"),
                       file_name="wacc_summary.csv", mime="text/csv")
    if not comp.empty:
        d3.download_button("All-industry comparison (CSV)", comp.to_csv(index=False).encode("utf-8"),
                           file_name="wacc_all_industries.csv", mime="text/csv")

    st.caption("Educational tool - not investment advice. Scraped sources can change format; "
               "verify key inputs and use the manual overrides when in doubt.")


if __name__ == "__main__":
    main()
