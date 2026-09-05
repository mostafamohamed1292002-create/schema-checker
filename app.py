import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    import extruct
    HAS_EXTRUCT = True
except ImportError:
    HAS_EXTRUCT = False


PASSWORD = "schema@test2026"

# ================== حماية بكلمة سر ==================
if "auth" not in st.session_state:
    st.session_state.auth = False

if not st.session_state.auth:
    st.set_page_config(page_title="Schema Checker", page_icon="🔍", layout="wide")
    st.title("🔍 Schema Checker")
    pw = st.text_input("🔑 اكتب كلمة السر", type="password")
    if pw:
        if pw == PASSWORD:
            st.session_state.auth = True
            st.rerun()
        else:
            st.error("كلمة السر غلط")
    st.stop()

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SchemaChecker/1.0)"}
SKIP_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf",
             ".zip", ".mp4", ".mp3", ".css", ".js", ".xml", ".ico")

# ================== قواعد الفحص (ضيف أي نوع جديد بنفس الشكل) ==================
SCHEMA_RULES = {
    "Article":       ["headline", "image", "datePublished", "author"],
    "NewsArticle":   ["headline", "image", "datePublished", "author"],
    "BlogPosting":   ["headline", "image", "datePublished", "author"],
    "Product":       ["name", "image"],
    "FAQPage":       ["mainEntity"],
    "HowTo":         ["name", "step"],
    "Recipe":        ["name", "image", "recipeIngredient", "recipeInstructions"],
    "Event":         ["name", "startDate", "location"],
    "LocalBusiness": ["name", "address"],
    "Restaurant":    ["name", "address"],
    "Store":         ["name", "address"],
    "Hotel":         ["name", "address"],
    "Organization":  ["name"],
    "BreadcrumbList":["itemListElement"],
    "WebSite":       ["name"],
    "WebPage":       ["name"],
    "VideoObject":   ["name", "description", "thumbnailUrl", "uploadDate"],
    "JobPosting":    ["title", "description", "hiringOrganization", "datePosted"],
    "Review":        ["itemReviewed", "reviewRating"],
    "Course":        ["name", "description", "provider"],
    "Person":        ["name"],
    "QAPage":        ["mainEntity"],
    "SoftwareApplication": ["name", "operatingSystem"],
    "Book":          ["name", "author"],
}

# ================== دوال مساعدة ==================
def as_list(x):
    if x is None: return []
    return x if isinstance(x, list) else [x]

def flatten_jsonld(obj):
    out = []
    if isinstance(obj, list):
        for o in obj: out.extend(flatten_jsonld(o))
    elif isinstance(obj, dict):
        if "@graph" in obj: out.extend(flatten_jsonld(obj["@graph"]))
        else: out.append(obj)
    return out

def extract_schemas(html):
    """يستخرج JSON-LD + Microdata من الصفحة"""
    schemas = []
    if HAS_EXTRUCT:
        try:
            data = extruct.extract(html, syntaxes=["json-ld", "microdata"], uniform=True)
            schemas.extend(flatten_jsonld(data.get("json-ld", [])))
            schemas.extend(data.get("microdata", []))
        except Exception:
            pass
    else:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                schemas.extend(flatten_jsonld(json.loads(tag.string or "")))
            except Exception:
                schemas.append({"_invalid_json": True})
    return schemas

# ================== فاحصات مخصصة ==================
def v_article(o):
    msgs = []
    for a in as_list(o.get("author")):
        if isinstance(a, dict) and not a.get("name"):
            msgs.append("author موجود بس ناقصه name")
    return msgs

def v_product(o):
    if not (o.get("offers") or o.get("aggregateRating") or o.get("review")):
        return ["لازم يحتوي على offers أو aggregateRating أو review على الأقل"]
    return []

def v_faq(o):
    msgs = []
    qs = as_list(o.get("mainEntity"))
    if not qs: return ["ناقص mainEntity"]
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict):
            msgs.append(f"السؤال رقم {i} شكله غير صحيح"); continue
        if not q.get("name"): msgs.append(f"السؤال رقم {i} ناقصه name")
        if not as_list(q.get("acceptedAnswer")): msgs.append(f"السؤال رقم {i} مالوش acceptedAnswer")
    return msgs

def v_breadcrumb(o):
    msgs = []
    items = as_list(o.get("itemListElement"))
    if not items: return ["ناقص itemListElement"]
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            msgs.append(f"العنصر رقم {i} شكله غير صحيح"); continue
        name = it.get("name") or (isinstance(it.get("item"), dict) and it["item"].get("name"))
        if not name: msgs.append(f"العنصر رقم {i} ناقصه name")
        if it.get("item") is None and it.get("url") is None:
            msgs.append(f"العنصر رقم {i} مالوش item أو url")
    return msgs

def v_howto(o):
    msgs = []
    steps = as_list(o.get("step"))
    if not steps: return ["ناقص step"]
    for i, s in enumerate(steps, 1):
        if isinstance(s, dict) and not (s.get("text") or s.get("name")):
            msgs.append(f"الخطوة رقم {i} ناقصها text أو name")
    return msgs

def v_rating(o):
    msgs = []
    for r in as_list(o.get("aggregateRating")):
        if isinstance(r, dict):
            if "ratingValue" not in r: msgs.append("aggregateRating ناقصه ratingValue")
            if "ratingCount" not in r and "reviewCount" not in r:
                msgs.append("aggregateRating ناقصه ratingCount أو reviewCount")
    return msgs

def v_review(o):
    r = o.get("reviewRating")
    if isinstance(r, dict) and "ratingValue" not in r:
        return ["reviewRating ناقصه ratingValue"]
    return []

CUSTOM_VALIDATORS = {
    "Article": v_article, "NewsArticle": v_article, "BlogPosting": v_article,
    "Product": v_product, "FAQPage": v_faq, "BreadcrumbList": v_breadcrumb,
    "HowTo": v_howto, "Review": v_review,
}

# ================== الفحص ==================
def validate_schema(obj):
    issues, types = [], []
    if not isinstance(obj, dict): return issues, types
    if obj.get("_invalid_json"):
        return ["فيه بلوك JSON-LD في الصفحة فيه JSON غير صالح"], types

    stype = obj.get("@type") or obj.get("type")
    if not stype:
        return ["البلوك مفيهوش @type"], types
    types = [str(t).rstrip("/").split("/")[-1] for t in as_list(stype)]

    context = str(obj.get("@context") or obj.get("context") or "https://schema.org")
    if "schema.org" not in context:
        issues.append(f"@context مش schema.org: '{context}'")

    for t in types:
        if t in SCHEMA_RULES:
            for field in SCHEMA_RULES[t]:
                if not obj.get(field):
                    issues.append(f"[{t}] الحقل الإجباري '{field}' ناقص")
            if t in CUSTOM_VALIDATORS:
                for m in CUSTOM_VALIDATORS[t](obj):
                    issues.append(f"[{t}] {m}")
    for m in v_rating(obj):
        issues.append(m)
    return issues, types

# ================== جمع الروابط ==================
def normalize_domain(d):
    d = d.strip()
    for p in ("https://", "http://"):
        if d.startswith(p): d = d[len(p):]
    d = d.rstrip("/")
    if d.startswith("www."): d = d[4:]
    return d

def fetch_sitemap(url, session, depth=0):
    urls = []
    try:
        r = session.get(url, timeout=15, headers=HEADERS)
        if r.status_code != 200: return urls
        soup = BeautifulSoup(r.content, "html.parser")
        for loc in soup.find_all("loc"):
            u = loc.get_text(strip=True)
            if not u: continue
            if u.lower().endswith(".xml") and depth < 3:
                urls.extend(fetch_sitemap(u, session, depth + 1))
            elif u.startswith("http"):
                urls.append(u)
    except Exception:
        pass
    return urls

def get_sitemap_urls(domain):
    session = requests.Session()
    for candidate in ["sitemap.xml", "sitemap_index.xml", "sitemap-index.xml", "wp-sitemap.xml"]:
        for scheme in ["https", "http"]:
            urls = fetch_sitemap(f"{scheme}://{domain}/{candidate}", session)
            if urls: return urls
    return []

def crawl_site(domain, max_pages):
    start = f"https://{domain}/"
    q, seen, found = deque([start]), {start}, []
    while q and len(found) < max_pages:
        url = q.popleft()
        try:
            r = requests.get(url, timeout=15, headers=HEADERS)
        except Exception:
            continue
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html").lower():
            continue
        found.append(url)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].split("#")[0]
            if not href or href.startswith(("mailto:", "tel:", "javascript:")): continue
            full = urljoin(url, href)
            if urlparse(full).netloc.lower() not in (domain.lower(), "www." + domain.lower()):
                continue
            if full not in seen:
                seen.add(full); q.append(full)
    return found

# ================== فحص صفحة واحدة ==================
def check_page(url):
    res = {"url": url, "schemas": [], "issues": [], "skip": False}
    try:
        r = requests.get(url, timeout=20, headers=HEADERS, allow_redirects=True)
    except Exception as e:
        res["issues"].append(f"مشكلة في الوصول للصفحة ({type(e).__name__})")
        return res
    if r.status_code != 200:
        res["issues"].append(f"الصفحة بترجع كود {r.status_code}")
        return res
    if "html" not in r.headers.get("Content-Type", "html").lower():
        res["skip"] = True
        return res

    schemas = extract_schemas(r.text)
    if not schemas:
        res["issues"].append("مفيش أي Schema Markup في الصفحة")
        return res

    all_issues, all_types = [], []
    for obj in schemas:
        iss, tps = validate_schema(obj)
        all_types.extend(tps)
        all_issues.extend(iss)
    res["schemas"] = sorted(set(all_types))
    res["issues"] = all_issues
    return res

# ================== الواجهة ==================
st.set_page_config(page_title="Schema Checker", page_icon="🔍", layout="wide")
st.title("🔍 Schema Checker")
st.caption("فحص سكيما كل صفحات الموقع — JSON-LD + Microdata")

with st.sidebar:
    st.header("⚙️ الإعدادات")
    domain = st.text_input("الدومين", placeholder="example.com")
    max_pages = st.number_input("أقصى عدد صفحات", 5, 1000, 50, 10)
    use_sitemap = st.checkbox("الاعتماد على sitemap.xml أولاً", True)
    workers = st.slider("عدد الفحوصات المتوازية", 1, 10, 5)

if st.button("🚀 ابدأ الفحص", type="primary"):
    d = normalize_domain(domain)
    if not d:
        st.error("اكتب الدومين الأول ✍️")
    else:
        with st.spinner("جاري جمع روابط الموقع..."):
            urls = get_sitemap_urls(d) if use_sitemap else []
            urls = [u for u in urls if not u.lower().endswith(SKIP_EXTS)][:max_pages]
            if not urls:
                st.info("مفيش sitemap — هجمع الروابط من لينكات الموقع...")
                urls = crawl_site(d, max_pages)

        if not urls:
            st.error("معرفتش أجمع روابط — اتأكد إن الدومين مكتوب صح والموقع شغال")
        else:
            st.info(f"🔗 اتجمع **{len(urls)}** رابط — الفحص شغال...")
            results, progress = [], st.progress(0)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(check_page, u) for u in urls]
                for i, f in enumerate(as_completed(futs), 1):
                    results.append(f.result())
                    progress.progress(int(i * 100 / len(urls)))
            progress.empty()

            results = [r for r in results if not r["skip"]]
            st.session_state["ok_pages"]  = sorted([r for r in results if not r["issues"]], key=lambda x: x["url"])
            st.session_state["bad_pages"] = sorted([r for r in results if r["issues"]], key=lambda x: x["url"])
            st.session_state["has_results"] = True

if st.session_state.get("has_results"):
    ok_pages  = st.session_state["ok_pages"]
    bad_pages = st.session_state["bad_pages"]

    st.success(f"✅ صفحات سليمة: **{len(ok_pages)}** — ⚠️ صفحات فيها مشاكل: **{len(bad_pages)}**")

    tab_ok, tab_bad = st.tabs([f"✅ الصفحات السليمة ({len(ok_pages)})", f"⚠️ صفحات فيها مشاكل ({len(bad_pages)})"])

    with tab_ok:
        if ok_pages:
            df = pd.DataFrame([{
                "الصفحة": r["url"],
                "أنواع السكيما": "، ".join(r["schemas"]) if r["schemas"] else "—"
            } for r in ok_pages])
            st.dataframe(df, use_container_width=True, height=400)
            st.download_button("⬇️ تحميل CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "healthy_pages.csv", "text/csv")
        else:
            st.warning("مفيش صفحات سليمة!")

    with tab_bad:
        if bad_pages:
            rows = []
            for r in bad_pages:
                for issue in r["issues"]:
                    rows.append({
                        "الصفحة": r["url"],
                        "أنواع السكيما": "، ".join(r["schemas"]) if r["schemas"] else "—",
                        "المشكلة": issue,
                    })
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, height=400)
            st.download_button("⬇️ تحميل CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "problem_pages.csv", "text/csv")
        else:
            st.balloons()
            st.success("مبروك! كل الصفحات سليمة 🎉")
