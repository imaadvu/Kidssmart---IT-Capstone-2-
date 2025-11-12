# extractor.py — industry-grade HTML → program rows (v2)
from __future__ import annotations

import json, re
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import dateparser

CURRENCY_MAP = {
    "$": "USD", "US$": "USD", "USD": "USD",
    "A$": "AUD", "AU$": "AUD", "AUD": "AUD",
    "£": "GBP", "GBP": "GBP",
    "€": "EUR", "EUR": "EUR",
    "INR": "INR", "₹": "INR",
}
EDU_WORDS = {w.lower() for w in [
    "course","class","workshop","training","tutorial","webinar","lecture","program",
    "degree","diploma","certificate","bootcamp","seminar","learn","education","study",
    "mooc","lesson","curriculum","module",
]}

PRICE_RE = re.compile(
    r'(?i)(?P<curr>USD|AUD|EUR|GBP|INR|US\$|AU\$|A\$|\$|£|€|₹)?\s*'
    r'(?P<amt1>(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?)\s*'
    r'(?:to|–|-|—|and)\s*'
    r'(?:(?P<curr_range>USD|AUD|EUR|GBP|INR|US\$|AU\$|A\$|\$|£|€|₹)?\s*)'
    r'(?P<amt2>(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?)?'
    r'(?P<curr2>USD|AUD|EUR|GBP|INR)?'
    r'|'
    r'(?P<curr_solo>USD|AUD|EUR|GBP|INR|US\$|AU\$|A\$|\$|£|€|₹)?\s*'
    r'(?P<amt_solo>(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?)\s*'
    r'(?P<curr2_solo>USD|AUD|EUR|GBP|INR)?'
)

def _clean_text(x: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(x)).strip() if x else ""

def _first(values: Iterable[Any]) -> Optional[Any]:
    for v in values:
        if v not in (None, "", [], {}): return v
    return None

def _to_iso(d: Optional[str]) -> Optional[str]:
    if not d: return None
    from datetime import datetime
    settings = {"PREFER_DATES_FROM":"future","STRICT_PARSING":False,"RETURN_AS_TIMEZONE_AWARE":False,
                "RELATIVE_BASE": datetime.now()}
    dt = dateparser.parse(str(d), settings=settings)
    if dt and dt.date() < datetime.now().date():
        alt = dateparser.parse(f"{d} {datetime.now().year + 1}", settings=settings)
        if alt and (alt - datetime.now()).days < 400: dt = alt
    return dt.strftime("%Y-%m-%d") if dt else None

def _norm_currency(cur: Optional[str]) -> Optional[str]:
    if not cur: return None
    cur = cur.strip().upper()
    return CURRENCY_MAP.get(cur, CURRENCY_MAP.get(cur.title(), cur))

def _extract_prices(text: str) -> List[Tuple[float, Optional[str]]]:
    out: List[Tuple[float, Optional[str]]] = []
    for m in PRICE_RE.finditer(text):
        amt = m.group("amt1") or m.group("amt_solo")
        cur = _norm_currency(_first([
            m.group("curr"), m.group("curr_range"), m.group("curr2"),
            m.group("curr_solo"), m.group("curr2_solo")
        ]))
        if not amt: continue
        try:
            v = float(amt.replace(",", "")); out.append((v, cur))
        except: pass
    return out

def _looks_educational(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in EDU_WORDS)

def _classify_type(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["youtube.com","vimeo.com","lecture","video"]): return "Video"
    if any(k in t for k in ["webinar","seminar","workshop","conference"]): return "Seminar"
    if any(k in t for k in ["course","bootcamp","mooc","degree","diploma","certificate"]): return "Course"
    return "Other"

def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type":"application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict): yield item
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    if isinstance(item, dict): yield item
            else:
                yield data

def _from_offers(offers) -> Tuple[Optional[float], Optional[str]]:
    if not offers: return (None, None)
    items = offers if isinstance(offers, list) else [offers]
    best_price, currency = None, None
    for it in items:
        try:
            p = it.get("price"); c = _norm_currency(it.get("priceCurrency"))
            if p is None: continue
            pv = float(str(p))
            if best_price is None or pv < best_price:
                best_price, currency = pv, c
        except: continue
    return (best_price, currency)

def _coerce_str(x: Any) -> Optional[str]:
    if x is None: return None
    if isinstance(x, (list, tuple)): return _clean_text(_first(x))
    return _clean_text(str(x))

def _entity_name(ent: Any) -> Optional[str]:
    if isinstance(ent, dict): return _coerce_str(ent.get("name") or ent.get("addressLocality"))
    return _coerce_str(ent)

def _country_from_addr(addr: Any) -> Optional[str]:
    if isinstance(addr, dict): return _coerce_str(addr.get("addressCountry"))
    return None

def _city_from_addr(addr: Any) -> Optional[str]:
    if isinstance(addr, dict): return _coerce_str(addr.get("addressLocality") or addr.get("addressRegion"))
    return None

def _rows_from_jsonld(obj: Dict[str, Any], base_url: str) -> List[Dict[str, Any]]:
    typ_raw = obj.get("@type", "")
    if isinstance(typ_raw, list): typ_raw = ",".join(typ_raw)
    typ = str(typ_raw).lower()
    rows: List[Dict[str, Any]] = []

    if any(k in typ for k in ["jobposting","person","organization","faqpage","article"]):
        return rows

    def add_row(name, desc, start, end, mode, venue, city, country, price, currency, url, rtype):
        url = url or base_url
        row = {
            "title": _clean_text(name) if name else None,
            "description": _clean_text(desc) if desc else None,
            "url": url, "price": price, "currency": currency,
            "start_date": _to_iso(start), "end_date": _to_iso(end),
            "mode": _coerce_str(mode) or None,
            "venue": _clean_text(venue) if venue else None,
            "city": _clean_text(city) if city else None,
            "country": _clean_text(country) if country else None,
            "type": rtype or None,
        }
        if row.get("title") or row.get("description"): rows.append(row)

    if "course" in typ or obj.get("@type") == ["LearningResource","Course"]:
        name = _coerce_str(obj.get("name"))
        desc = _coerce_str(obj.get("description"))
        url = _coerce_str(obj.get("url") or obj.get("mainEntityOfPage")) or base_url
        start = _coerce_str(obj.get("startDate")); end = _coerce_str(obj.get("endDate"))
        mode = _coerce_str(obj.get("courseMode"))
        org = obj.get("provider") or obj.get("organizer")
        venue = _entity_name(org); city = None; country = None
        price, currency = _from_offers(obj.get("offers"))
        add_row(name, desc, start, end, mode, venue, city, country, price, currency, url, "Course")
    elif "event" in typ:
        name = _coerce_str(obj.get("name")); desc = _coerce_str(obj.get("description"))
        url = _coerce_str(obj.get("url")) or base_url
        start = _coerce_str(obj.get("startDate")); end = _coerce_str(obj.get("endDate"))
        mode = _coerce_str(obj.get("eventAttendanceMode"))
        loc = obj.get("location") or {}; venue = _entity_name(loc)
        addr = loc.get("address") if isinstance(loc, dict) else None
        city = _city_from_addr(addr); country = _country_from_addr(addr)
        price, currency = _from_offers(obj.get("offers"))
        add_row(name, desc, start, end, mode, venue, city, country, price, currency, url, "Seminar")
    elif any(k in typ for k in ["creativework","learningresource"]):
        name = _coerce_str(obj.get("name")); desc = _coerce_str(obj.get("description"))
        url = _coerce_str(obj.get("url")) or base_url
        add_row(name, desc, None, None, None, None, None, None, None, None, url, None)

    return rows

def _rows_from_microdata(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in soup.select('[itemscope][itemtype]'):
        itype = (item.get('itemtype') or '').lower()
        if any(k in itype for k in ["course","event","education"]):
            name_tag = item.select_one('[itemprop=name]')
            desc_tag = item.select_one('[itemprop=description]')
            url_tag = item.select_one('[itemprop=url]')
            start_tag = item.select_one('[itemprop=startDate]')
            end_tag = item.select_one('[itemprop=endDate]')
            price_tag = item.select_one('[itemprop=price]')
            curr_tag = item.select_one('[itemprop=priceCurrency]')
            venue_tag = item.select_one('[itemprop=location] [itemprop=name], [itemprop=organizer] [itemprop=name]')
            city_tag = item.select_one('[itemprop=addressLocality]')
            country_tag = item.select_one('[itemprop=addressCountry]')

            title = _clean_text(name_tag.get_text() if name_tag else None)
            desc = _clean_text(desc_tag.get_text() if desc_tag else None)
            url2 = _clean_text(url_tag.get('href') if url_tag else base_url) or base_url
            if url2 and not urlparse(url2).netloc: url2 = urljoin(base_url, url2)

            price = None
            if price_tag and price_tag.get_text():
                try: price = float(price_tag.get_text().strip())
                except: price = None
            currency = _norm_currency(curr_tag.get_text() if curr_tag else None)

            rows.append({
                "title": title or None, "description": desc or None, "url": url2,
                "price": price, "currency": currency,
                "start_date": _to_iso(start_tag.get('content') if start_tag else None) or _to_iso(start_tag.get_text() if start_tag else None),
                "end_date": _to_iso(end_tag.get('content') if end_tag else None) or _to_iso(end_tag.get_text() if end_tag else None),
                "mode": None,
                "venue": _clean_text(venue_tag.get_text() if venue_tag else None) or None,
                "city": _clean_text(city_tag.get_text() if city_tag else None) or None,
                "country": _clean_text(country_tag.get_text() if country_tag else None) or None,
                "type": _classify_type(itype),
            })
    return rows

def _rows_from_lists(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    selectors = [
        "main li","main div.course-item","main div.event-card",
        ".course-list > div",".events-grid > *","section li",
        "ul.course-list > li","ol.course-list > li","table tr"
    ]
    potential_items = soup.select(",".join(selectors))
    for item in potential_items[:60]:
        title_tag = item.find(["h2","h3","h4","a"], string=lambda t: t and len(t.split())>1)
        link_tag = item.find("a", href=True)
        if not title_tag or not link_tag: continue
        title = _clean_text(title_tag.get_text())
        u = urljoin(base_url, link_tag["href"])
        p = urlparse(u)
        if not p.netloc or p.fragment: continue
        desc_tag = item.find(["p","div"], string=lambda t: t and len(t.split())>5)
        desc = _clean_text(desc_tag.get_text()) if desc_tag else None
        combined = f"{title} {desc or ''}"
        if not _looks_educational(combined): continue
        prices = _extract_prices(item.get_text(" "))
        price, currency = prices[0] if prices else (None, None)
        mode = "Online" if any(k in item.get_text(" ").lower() for k in ["online","virtual"]) else None
        rows.append({
            "title": title, "description": desc, "url": u,
            "price": price, "currency": currency,
            "start_date": None, "end_date": None, "mode": mode,
            "venue": None, "city": None, "country": None,
            "type": _classify_type(combined)
        })
    return rows

def _rows_from_fallbacks(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    og_title = soup.find("meta", property="og:title")
    og_desc = soup.find("meta", property="og:description")
    tw_title = soup.find("meta", attrs={"name":"twitter:title"})
    tw_desc = soup.find("meta", attrs={"name":"twitter:description"})
    page_title = _clean_text(soup.title.get_text()) if soup.title and soup.title.get_text() else None
    title = _first([og_title.get("content") if og_title else None,
                    tw_title.get("content") if tw_title else None,
                    page_title])
    desc = _first([og_desc.get("content") if og_desc else None,
                   tw_desc.get("content") if tw_desc else None])
    text = _clean_text(soup.get_text(" "))
    if not _looks_educational(text): return []
    prices = _extract_prices(text); price, currency = (prices[0] if prices else (None, None))
    iso_date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    start_date = _to_iso(iso_date.group(1)) if iso_date else None
    rows.append({
        "title": title or page_title or "Program",
        "description": desc or None, "url": base_url,
        "price": price, "currency": currency,
        "start_date": start_date, "end_date": None,
        "mode": "Online" if "online" in text.lower() else None,
        "venue": None, "city": None, "country": None,
        "type": _classify_type(text),
    })
    return rows

def _dedupe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set(); out = []
    for r in rows:
        key = (str(r.get("title","")).strip().lower(), str(r.get("url","")).strip().lower())
        if key in seen: continue
        seen.add(key); out.append(r)
    return out

def extract_programs(html_raw: str, url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not html_raw: return rows
    soup = BeautifulSoup(html_raw, "html.parser")
    for obj in _iter_jsonld(soup): rows.extend(_rows_from_jsonld(obj, url))
    rows.extend(_rows_from_microdata(soup, url))
    if len(rows) < 5: rows.extend(_rows_from_lists(soup, url))
    if not rows: rows.extend(_rows_from_fallbacks(soup, url))
    nr: List[Dict[str, Any]] = []
    for r in rows:
        r["title"] = _clean_text(r.get("title")) or "Program"
        r["description"] = _clean_text(r.get("description")) or None
        r["url"] = r.get("url") or url
        r["currency"] = _norm_currency(r.get("currency")) if r.get("currency") else r.get("currency")
        mode = (r.get("mode") or "").lower()
        if any(k in mode for k in ["online","virtual","remote"]): r["mode"]="Online"
        elif any(k in mode for k in ["inperson","in-person","campus","onsite","on-site","classroom"]): r["mode"]="In-person"
        else: r["mode"]= r.get("mode") or "Unknown"
        r["type"] = r.get("type") or _classify_type(" ".join([r.get("title",""), r.get("description","")]))
        nr.append(r)
    nr = _dedupe(nr)
    if len(nr) > 30: nr = nr[:30]
    return nr
