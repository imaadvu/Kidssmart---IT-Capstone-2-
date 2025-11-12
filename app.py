# streamlit_app.py — KidsSmart+ (no search_scrape import; secrets-safe; login-gated links)

import os
from typing import Optional

import requests
import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
from serpapi import GoogleSearch
import dateparser

from database import (
    create_database, save_query, save_program_rows,
    list_programs, get_program_detail, toggle_program_approved, quick_stats,
    create_user, verify_user
)
from extractor import extract_programs

# ---------------- CONFIG ----------------
st.set_page_config(page_title="KidsSmart+ Educational Database", layout="wide")
create_database()

LOGO = "logo2.png"

# Secrets/env → do NOT hardcode keys
API_KEY = st.secrets.get("SERPAPI_API_KEY") or os.environ.get("SERPAPI_API_KEY") or ""
if not API_KEY:
    st.warning("SERPAPI_API_KEY not found (add in .streamlit/secrets.toml locally, or in Streamlit Cloud → Settings → Secrets).")

USE_LLM_EXTRACTION = False  # hook disabled by default
EXCLUSION_TERMS = ["-jobs", "-careers", "-employment", "-hire", "-vacancy", "-scholarship"]

COUNTRY_REGIONS = {
    "Any": ["Any"],
    "Australia": ["Any", "Melbourne", "Sydney", "Brisbane", "Perth", "Adelaide"],
    "United States": ["Any", "New York", "Los Angeles", "Chicago", "San Francisco"],
    "United Kingdom": ["Any", "London", "Manchester", "Birmingham"],
    "Canada": ["Any", "Toronto", "Vancouver", "Montreal"],
    "India": ["Any", "Mumbai", "Delhi", "Bengaluru", "Chennai"],
}

EXCHANGE_RATES = {"USD": 1.0, "AUD": 0.65, "GBP": 1.25, "EUR": 1.08, "INR": 0.012}

# ---------------- ACCOUNT (sidebar + popup support) ----------------
def account_box():
    with st.sidebar:
        try:
            st.image(LOGO, width=200)
        except Exception:
            pass

        st.markdown("### Account")
        if "user" not in st.session_state:
            tab_login, tab_register = st.tabs(["Login", "Register"])

            with tab_login:
                email = st.text_input("Email", key="login_email")
                pwd = st.text_input("Password", type="password", key="login_pwd")
                if st.button("Sign in"):
                    user = verify_user(email, pwd)
                    if user:
                        st.session_state["user"] = user
                        st.success(f"Welcome {user['name']}!")
                        st.rerun()
                    else:
                        st.error("Invalid credentials.")

            with tab_register:
                r_email = st.text_input("Email", key="reg_email")
                r_name = st.text_input("Name (optional)", key="reg_name")
                r_pwd = st.text_input("Password", type="password", key="reg_pwd")
                if st.button("Create account"):
                    ok, msg = create_user(r_email, r_name, r_pwd, role="user")
                    if ok:
                        st.success("Account created. Please login.")
                    else:
                        st.error(msg)
        else:
            u = st.session_state["user"]
            st.write(f"**Logged in as:** {u['email']} ({u['role']})")
            if st.button("Logout"):
                st.session_state.pop("user", None)
                st.rerun()

account_box()
def current_user():
    return st.session_state.get("user")

# --- flash + per-card expanded state ---
if "flash" in st.session_state:
    st.warning(st.session_state.pop("flash"))

def is_expanded(i: int) -> bool:
    return st.session_state.get(f"expanded_{i}", False)

def set_expanded(i: int, val: bool):
    st.session_state[f"expanded_{i}"] = val

# ---------------- POPUP AUTH (modal/dialog) ----------------
_dialog = getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None)
HAS_DIALOG = bool(getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None))

def open_auth_modal():
    if _dialog is None:
        st.session_state["flash"] = "Please login or create an account in the sidebar."
        return

    @_dialog("Sign in to continue")
    def _auth():
        st.write("Create an account or sign in to continue.")
        tab_login, tab_register = st.tabs(["Login", "Register"])

        with tab_login:
            email = st.text_input("Email", key="modal_login_email")
            pwd = st.text_input("Password", type="password", key="modal_login_pwd")
            if st.button("Sign in", key="modal_signin_btn"):
                user = verify_user(email, pwd)
                if user:
                    st.session_state["user"] = user
                    idx = st.session_state.get("auth_target_idx")
                    if idx is not None:
                        set_expanded(idx, True)
                        st.session_state.pop("auth_target_idx", None)
                    st.session_state["show_auth_modal"] = False
                    st.rerun()
                else:
                    st.error("Invalid credentials.")

        with tab_register:
            r_email = st.text_input("Email", key="modal_reg_email")
            r_name = st.text_input("Name (optional)", key="modal_reg_name")
            r_pwd = st.text_input("Password", type="password", key="modal_reg_pwd")
            if st.button("Create account", key="modal_register_btn"):
                ok, msg = create_user(r_email, r_name, r_pwd, role="user")
                if ok:
                    st.success("Account created. Please sign in.")
                else:
                    st.error(msg)

if st.session_state.get("show_auth_modal") and HAS_DIALOG:
    open_auth_modal()

# ---------------- HELPERS ----------------
EDU_KEYWORDS = [
    "course", "class", "workshop", "training", "tutorial",
    "webinar", "lecture", "program", "degree", "diploma",
    "certificate", "bootcamp", "seminar", "learn", "education", "study"
]

def is_educational(text: str) -> bool:
    t = text.lower()
    return any(word in t for word in EDU_KEYWORDS)

def classify_type(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["webinar", "seminar", "workshop"]): return "Seminar"
    if any(k in t for k in ["video", "youtube", "lecture"]):    return "Video"
    if any(k in t for k in ["course", "bootcamp", "mooc"]):     return "Course"
    return "Other"

def matches_location(combined_text: str, country: str, region: str) -> bool:
    text = combined_text.lower()
    if country == "Any": return True
    c = country.lower(); r = region.lower()
    if region == "Any": return c in text
    return (c in text) or (r in text)

def normalize_date(val: Optional[str]) -> Optional[str]:
    if not val: return None
    dt = dateparser.parse(val)
    return dt.strftime("%Y-%m-%d") if dt else None

def get_usd_price(price: Optional[float], currency: Optional[str]) -> Optional[float]:
    if price is None or not currency: return None
    rate = EXCHANGE_RATES.get(currency.upper())
    if not rate: return None
    return float(price) * (EXCHANGE_RATES["USD"] / rate)

# ---- NETWORK / PARSING ----
def fetch_html(url: str) -> str:
    """Return RAW HTML (do NOT strip tags) so extractor can read JSON-LD."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text
    except Exception:
        return ""

def html_to_text(html: str, max_chars: int = 200000) -> str:
    """Readable text for relevance checks and previews."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for s in soup(["script", "style", "noscript"]): s.decompose()
        return " ".join(soup.get_text(" ").split())[:max_chars]
    except Exception:
        return ""

def get_page_title_from_html(html: str) -> Optional[str]:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        if soup.title and soup.title.get_text():
            return " ".join(soup.title.get_text().split())
    except Exception:
        pass
    return None

def _run_serpapi_query(query: str, max_results: int):
    if not API_KEY:
        raise RuntimeError("SERPAPI_API_KEY missing")
    params = {"engine": "google", "q": query, "num": max_results, "api_key": API_KEY}
    data = GoogleSearch(params).get_dict()
    organic = data.get("organic_results", [])[:max_results]
    out = []
    for r in organic:
        title = r.get("title", "")
        link = r.get("link", "")
        snippet = r.get("snippet", "")
        if link:
            out.append({"title": title, "link": link, "snippet": snippet})
    return out

def search_web(topic: str, filters: dict, max_results: int = 8):
    if not API_KEY:
        st.error("Missing SERPAPI_API_KEY; cannot run Google search.")
        return []

    base_parts = [topic, "education", "course OR workshop OR webinar OR training"]
    base_parts.extend(EXCLUSION_TERMS)

    if filters["type"] == "Course":  base_parts.append("course")
    if filters["type"] == "Seminar": base_parts.append("seminar OR workshop")
    if filters["type"] == "Video":   base_parts.append("video OR lecture")
    if filters["mode"] == "Online":    base_parts.append("online")
    if filters["mode"] == "In-person": base_parts.append("in person OR on campus")
    if filters["cost"] == "Free":           base_parts.append("free")
    if filters["cost"] == "Paid / Unknown": base_parts.append("fee OR $")

    country, region = filters["country"], filters["region"]
    tries = []
    if country != "Any" and region != "Any": tries.append(base_parts + [country, region])
    if country != "Any":                     tries.append(base_parts + [country])
    tries.append(base_parts)

    for parts in tries:
        q = " ".join(parts)
        try:
            res = _run_serpapi_query(q, max_results)
        except Exception as e:
            st.error(f"Search error: {e}")
            res = []
        if res:
            return res
    return []

def llm_extract(topic: str, html: str, url: str) -> Optional[list[dict]]:
    if not USE_LLM_EXTRACTION:
        return None
    return None  # placeholder for future

def preview_5_words(text: str) -> str:
    if not text: return ""
    words = text.split()
    return " ".join(words[:5]) + ("…" if len(words) > 5 else "")

# ---------------- NAV ----------------
user = current_user()
pages = ["Find Programs", "Programs"]
if user and user.get("role") == "admin":
    pages.append("Admin")
page = st.sidebar.radio("Navigate", pages)

# ---------------- PAGE: FIND PROGRAMS ----------------
if page == "Find Programs":
    st.markdown("<h1 style='font-size:32px;font-weight:bold;margin-bottom:0;'>KidsSmart+</h1>", unsafe_allow_html=True)
    st.markdown("<p style='font-size:18px;margin-top:4px;'>The one-stop place to find Educational Programs, Courses & Videos</p>", unsafe_allow_html=True)

    c1, c2 = st.columns([3, 1])
    with c1:
        topic = st.text_input("What do you want to learn?", placeholder="e.g. Early childhood literacy, Python for beginners, VCE maths")
    with c2:
        num_results = st.slider("Max results", 3, 15, 8)

    c3, c4, c5 = st.columns(3)
    with c3:
        type_filter = st.selectbox("Resource type", ["Any", "Course", "Seminar", "Video", "Other"])
    with c4:
        mode_filter = st.selectbox("Delivery mode", ["Any", "Online", "In-person"])
    with c5:
        cost_filter = st.selectbox("Cost", ["Any", "Free", "Paid / Unknown"])

    c6, c7 = st.columns(2)
    with c6:
        country = st.selectbox("Country", list(COUNTRY_REGIONS.keys()), index=0)
    with c7:
        region = st.selectbox("Region / City", COUNTRY_REGIONS[country], index=0)

    filters = {"type": type_filter, "mode": mode_filter, "cost": cost_filter, "country": country, "region": region}

    # ---- Search button: scrape & extract, then persist to session_state
    if st.button("Search"):
        if not topic.strip():
            st.warning("Please enter a topic.")
        else:
            uid = user["id"] if user else None
            save_query(uid, topic, filters)

            with st.spinner("Searching Google…"):
                results = search_web(topic, filters, max_results=num_results)

            if not results:
                st.error("No search results (even after relaxing location). Try different filters.")
            else:
                st.info(f"Found {len(results)} results. Scraping & extracting…")
                prog = st.progress(0.0)
                total = len(results)
                all_rows = []

                for i, r in enumerate(results, start=1):
                    # --- fetch RAW HTML for extractor ---
                    html_raw = fetch_html(r["link"])
                    page_title = get_page_title_from_html(html_raw) or r["title"] or "Program"
                    text = html_to_text(html_raw)

                    combined = f"{r['title']} {r.get('snippet','')} {text}"
                    if not is_educational(combined):
                        prog.progress(i/total); continue
                    if not matches_location(combined, country, region):
                        prog.progress(i/total); continue

                    # Try LLM first (if enabled) else structured extractor
                    rows = llm_extract(topic, html_raw, r["link"]) or extract_programs(html_raw, r["link"])

                    # normalize & ensure a real title
                    for row in rows:
                        # Title fallback chain: extractor → SERPAPI title → <title> → "Program"
                        if not row.get("title") or row.get("title") == "Program":
                            row["title"] = (r["title"] or page_title or "Program")[:140]

                        if not row.get("type") or row["type"] == "Other":
                            row["type"] = classify_type(combined)
                        if not row.get("mode") or row["mode"] == "Unknown":
                            row["mode"] = "Online" if "online" in combined.lower() else row.get("mode", "Unknown")
                        row["start_date"] = normalize_date(row.get("start_date"))
                        row["end_date"]   = normalize_date(row.get("end_date"))
                        if not row.get("country") and country != "Any": row["country"] = country
                        if not row.get("city")    and region  != "Any": row["city"]    = region
                        row["price_usd"] = get_usd_price(row.get("price"), row.get("currency"))
                    all_rows.extend(rows)
                    prog.progress(i/total)

                if not all_rows:
                    st.warning("Pages scraped, but nothing matched all filters.")
                else:
                    save_program_rows(all_rows)
                    st.success(f"Saved {len(all_rows)} program entries ✅")

                    # persist results for reruns (so See more/modal works)
                    st.session_state['last_search_results'] = all_rows
                    st.session_state['last_search_topic'] = topic

                    # reset expand toggles on a new search
                    for k in [k for k in list(st.session_state.keys()) if k.startswith("expanded_")]:
                        del st.session_state[k]

                    st.rerun()

    # If they just logged in after clicking a locked link, show a helper
    if current_user() and st.session_state.get("pending_open_url"):
        st.success("You're logged in — link unlocked:")
        url_to_open = st.session_state.get("pending_open_url")
        st.markdown(f"[Open: {url_to_open}]({url_to_open})")
        st.session_state.pop("pending_open_url", None)

    # ---- Display results from session_state on every rerun
    if 'last_search_results' in st.session_state and st.session_state['last_search_results']:
        all_rows = st.session_state['last_search_results']
        topic_label = st.session_state.get('last_search_topic', 'Programs')

        st.markdown(f"### 📋 Extracted Programs for: *{topic_label}*")
        show = min(12, len(all_rows))
        cols = st.columns(3)

        user = current_user()
        for i in range(show):
            c = cols[i % 3]
            with c:
                item = all_rows[i]
                title = item.get('title','Program')
                desc  = (item.get('description') or '')
                url   = item['url']

                # --- Title / link (login required) ---
                if not user:
                    if st.button(f"🔗 {title} (login to open)", key=f"login_to_open_{i}"):
                        st.session_state["auth_target_idx"] = i
                        st.session_state["auth_inline"] = i
                        st.session_state["pending_open_url"] = url
                        st.rerun()
                else:
                    st.markdown(f"**[{title}]({url})**")

                st.caption(
                    f"Type: {item.get('type','')} | Mode: {item.get('mode','')} | "
                    f"{item.get('country','')}{' - ' + item.get('city','') if item.get('city') else ''}"
                )
                if item.get("price") is not None:
                    st.caption(f"Price: {item['price']} {item.get('currency','')} (≈ USD {(item.get('price_usd') or 0):.2f})")

                preview = preview_5_words(desc)
                st.write(preview)

                # --- See more / Hide with inline auth fallback (always works) ---
                if not is_expanded(i):
                    if st.button("See more", key=f"see_{i}"):
                        if not user:
                            st.session_state["auth_target_idx"] = i
                            st.session_state["auth_inline"] = i   # force inline auth
                            st.rerun()
                        else:
                            set_expanded(i, True)
                            st.rerun()
                else:
                    st.write(desc if len(desc) < 1200 else desc[:1200] + "…")
                    if st.button("Hide", key=f"hide_{i}"):
                        set_expanded(i, False)
                        st.rerun()

                # Inline login card (works on all Streamlit versions)
                if not user and st.session_state.get("auth_inline") == i:
                    with st.container():
                        st.markdown("**Sign in to view full details**")
                        with st.form(key=f"inline_login_{i}", clear_on_submit=False):
                            email = st.text_input("Email", key=f"inline_email_{i}")
                            pwd = st.text_input("Password", type="password", key=f"inline_pwd_{i}")
                            c1, c2 = st.columns(2)
                            do_login = c1.form_submit_button("Sign in")
                            do_cancel = c2.form_submit_button("Cancel")

                        if do_login:
                            u = verify_user(email, pwd)
                            if u:
                                st.session_state["user"] = u
                                set_expanded(i, True)
                                st.session_state.pop("auth_inline", None)
                                st.success(f"Welcome {u['name']}!")
                                st.rerun()
                            else:
                                st.error("Invalid credentials.")
                        elif do_cancel:
                            st.session_state.pop("auth_inline", None)
                            st.rerun()

                st.markdown("---")

# ---------------- PAGE: PROGRAMS ----------------
elif page == "Programs":
    st.markdown("## 📚 Program Library")
    ftype = st.selectbox("Type", ["Any","Course","Seminar","Video","Other"])
    fmode = st.selectbox("Mode", ["Any","Online","In-person","Unknown"])
    fcost = st.selectbox("Cost", ["Any","Free","Paid / Unknown"])
    fcountry = st.text_input("Country contains")
    fcity = st.text_input("City contains")

    rows = list_programs({
        "type": ftype, "mode": fmode, "cost": fcost,
        "country_contains": fcountry, "city_contains": fcity
    })

    if not rows:
        st.info("No programs found. Use Find Programs to scrape more.")
    else:
        df = pd.DataFrame(rows, columns=["ID","Title","Type","Mode","Country","City","Price","Currency","URL"])
        st.dataframe(df, use_container_width=True)

        pid = st.number_input("View Program ID (login required)", min_value=1, step=1)
        if st.button("Open details"):
            user = current_user()
            if not user:
                st.session_state["auth_target_idx"] = None
                if HAS_DIALOG:
                    st.session_state["show_auth_modal"] = True
                else:
                    st.session_state["auth_inline"] = -1  # generic inline card
                st.rerun()
            else:
                detail = get_program_detail(int(pid))
                if detail:
                    (pid, url, title, desc, price, curr, price_usd, start, end,
                     mode, venue, city, country, typ, approved, created) = detail
                    st.markdown(f"### {title}")
                    st.write(desc or "(no description)")
                    st.write(f"**Type:** {typ} | **Mode:** {mode}")
                    st.write(f"**When:** {start or '-'} → {end or '-'}")
                    st.write(f"**Where:** {venue or '-'}, {city or '-'}, {country or '-'}")
                    if price is not None:
                        usd_txt = f" (≈ USD {price_usd:.2f})" if price_usd is not None else ""
                        st.write(f"**Price:** {price} {curr or ''}{usd_txt}")
                    st.write(f"[Open source page]({url})")
                else:
                    st.warning("Program not found.")

# ---------------- PAGE: ADMIN ----------------
elif page == "Admin":
    user = current_user()
    if not user or user.get("role") != "admin":
        st.error("Admins only.")
    else:
        st.markdown("## 🔧 Admin Dashboard")
        s = quick_stats()
        if s:
            st.write({"programs_total": s[0], "programs_approved": s[1], "sources_total": s[2]})

        pid = st.number_input("Program ID to toggle approve/unapprove", min_value=1, step=1)
        if st.button("Toggle approve"):
            toggle_program_approved(int(pid))
            st.success("Toggled approval state.")

# ---------------- FOOTER ----------------
st.markdown("""
<div style="position:fixed;bottom:0;left:0;width:100%;background:#000;color:#fff;text-align:center;font-size:14px;padding:10px;z-index:999;">
Created by <b>Mohamed Imaad Muhinudeen (s8078260)</b> & <b>Kavin Nanthakumar (s8049341)</b> | All Rights Reserved | KidsSmart+
</div>
""", unsafe_allow_html=True)
