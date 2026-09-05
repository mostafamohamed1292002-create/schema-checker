import json
import time
import random
import threading
from collections import deque, Counter
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

# ⚠️⚠️⚠️ حط كلمة السر بتاعتك هنا ⚠️⚠️⚠️
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

# متصفح وهمي بشكل متصفح حقيقي عشان الحمايات ما تحجبش الفحص
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SKIP_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf",
             ".zip", ".mp4", ".mp3", ".css", ".js", ".xml", ".ico")

# ================== تبطيء الطلبات عشان الموقع مايزعلش ==================
MIN_DELAY = 0.5  # أقل فترة بين كل طلب والتاني (بال ثانية)
_request_lock = threading.Lock()
_last_request = [0.0]

def polite_get(url, timeout=25):
    """بياخد مسافة بسيطة بين الطلبات عشان ما نعملش 429"""
    with _request_lock:
        passed = time.time() - _last_request[0]
        if passed < MIN_DELAY:
            time.sleep(MIN_DELAY - passed + random.uniform(0, 0.15))
        _last_request[0] = time.time()
    return requests.get(url, timeout=timeout, headers=HEADERS, allow_redirects=True)

# ================== شرح أكواد الأخطاء بالبلدي ==================
STATUS_HINTS = {
    401: "الصفحة محتاجة تسجيل دخول (Unauthorized)",
    403: "الموقع رافض الوصول (Forbidden) — غالبًا حماية زي Cloudflare واقفة الفحص، جرب تبطئ السرعة وتاني",
    404: "الصفحة مش موجودة (Not Found) — الرابط بايظ أو الصفحة اتمسحت",
    408: "السيرفر أخد وقت طويل وقطع الاتصال (Timeout)",
    410: "الصفحة اتمسحت نهائيًا من الموقع (Gone)",
    429: "الفحص كان أسرع من تحمّل الموقع (Too Many Requests) — قلل 'عدد الفحوصات المتوازية' وزوّد 'التأخير بين الطلبات' وبعدين اضغط زرار إعادة الفحص",
    500: "خطأ جوه سيرفر الموقع نفسه (Server Error) — مش من الأداة",
    502: "مشكلة في بوابة الموقع (Bad Gateway) — غالبًا مؤقتة، جرب تاني",
    503: "السيرفر مش متاح دلوقتي (Service Unavailable) — صيانة أو ضغط على الموقع",
    504: "السيرفر أخد وقت طويل في الرد (Gateway Timeout) — جرب تاني",
}

def status_message(code):
    hint = STATUS_HINTS.get(code, "كود غير معتاد من السيرفر")
    return f"الصفحة رجعت كود {code} — {hint}"

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

def fetch_sitemap(url, depth=0):
    urls = []
    try:
        r = polite_get(url, timeout=15)
        if r.status_code != 200: return urls
        soup = BeautifulSoup(r.content, "html.parser")
        for loc in soup.find_all("loc"):
            u = loc.get_text(strip=True)
            if not u: continue
            if u.lower().endswith(".xml") and depth < 3:
                urls.extend(fetch_sitemap(u, depth + 1))
            elif u.startswith("http"):
                urls.append(u)
    except Exception:
        pass
    return urls

def get_sitemap_urls(domain):
    for candidate in ["sitemap.xml", "sitemap_index.xml", "sitemap-index.xml", "wp-sitemap.xml"]:
        for scheme in ["https", "http"]:
            urls = fetch_sitemap(f"{scheme}://{domain}/{candidate}")
            if urls: return urls
    return []

def crawl_site(domain, max_pages):
    start = f"https://{domain}/"
    q, seen, found = deque([start]), {start}, []
    while q and len(found) < max_pages:
        url = q.popleft()
        try:
            r = polite_get(url, timeout=15)
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

# ================== فحص صفحة واحدة (مع إعادة محاولة) ==================
def check_page(url, max_retries=3):
    res = {"url": url, "schemas": [], "issues": [], "skip": False, "access_error": False}
    code, r = None, None

    for attempt in range(1, max_retries + 1):
        try:
            r = polite_get(url)
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                time.sleep(3 * attempt); continue
            res["issues"].append("الاتصال بالصفحة اتأخر جدًا (Timeout) — السيرفر ماردش في الوقت المحدد، جرب تاني")
            res["access_error"] = True
            return res
        except requests.exceptions.SSLError:
            res["issues"].append("مشكلة في شهادة الحماية SSL للموقع")
            res["access_error"] = True
            return res
        except Exception as e:
            if attempt < max_retries:
                time.sleep(3 * attempt); continue
            res["issues"].append(f"معرفش أوصل للصفحة ({type(e).__name__}) — افتح اللينك في المتصفح واتأكد إنه شغال")
            res["access_error"] = True
            return res

        code = r.status_code
        if code == 200:
            break
        if code == 429 or code >= 500:
            # استنى شوية وجرب تاني — الموقع بيصبرنا
            retry_after = str(r.headers.get("Retry-After", ""))
            wait = int(retry_after) if retry_after.isdigit() else 4 * attempt
            time.sleep(min(wait, 25))
            continue
        break  # 404 / 403 — مفيش فايدة من إعادة المحاولة

    if code is None:
        return res
    if code != 200:
        res["issues"].append(status_message(code))
        res["access_error"] = True
        return res

    if "html" not in r.headers.get("Content-Type", "html").lower():
        res["skip"] = True
        return res

    schemas = extract_schemas(r.text)
    if not schemas:
        res["issues"].append("مفيش أي Schema Markup في الصفحة — دي صفحة محتاجة سكيما")
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
    workers = st.slider("عدد الفحوصات المتوازية", 1, 10, 3, 1)
    delay = st.slider("التأخير بين الطلبات (ثانية)", 0.0, 3.0, 0.5, 0.1)
    st.caption("💡 لو ظهرت صفحات كتير بكود 429: قلل الفحوصات المتوازية لـ 1 وزود التأخير لـ 1.5")

if st.button("🚀 ابدأ الفحص", type="primary"):
    d = normalize_domain(domain)
    if not d:
        st.error("اكتب الدومين الأول ✍️")
    else:
        MIN_DELAY = delay  # تطبيق التأخير اللي اختاره المستخدم
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

# عرض النتائج
if st.session_state.get("has_results"):
    ok_pages  = st.session_state["ok_pages"]
    bad_pages = st.session_state["bad_pages"]

    st.success(f"✅ صفحات سليمة: **{len(ok_pages)}** — ⚠️ صفحات فيها مشاكل: **{len(bad_pages)}**")

    n_rate = sum(1 for r in bad_pages if any("429" in i for i in r["issues"]))
    if n_rate:
        st.warning(
            f"⚠️ في **{n_rate}** صفحة رجعت كود 429 — ده معناه إن الفحص أسرع من تحمّل الموقع والصفحات دي متفحصتش أصلًا. "
            "قلل 'عدد الفحوصات المتوازية' لـ 1 وزوّد 'التأخير بين الطلبات' لـ 1.5 ثانية وبعدين اضغط زرار إعادة الفحص اللي تحت."
        )

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
            with st.expander("📊 ملخص المشاكل — كل مشكلة ظهرت كام مرة"):
                cnt = Counter()
                for rr in bad_pages:
                    for i in set(rr["issues"]):
                        cnt[i] += 1
                st.dataframe(pd.DataFrame(cnt.most_common(), columns=["المشكلة", "عدد الصفحات"]),
                             use_container_width=True)

            rows = []
            for r in bad_pages:
                if r["schemas"]:
                    t = "، ".join(r["schemas"])
                elif r.get("access_error"):
                    t = "لم يتم الفحص (الصفحة مارجعتش)"
                else:
                    t = "—"
                for issue in r["issues"]:
                    rows.append({"الصفحة": r["url"], "أنواع السكيما": t, "المشكلة": issue})
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, height=400)
            st.download_button("⬇️ تحميل CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               "problem_pages.csv", "text/csv")

            failed_urls = [r["url"] for r in bad_pages if r.get("access_error")]
            if failed_urls:
                if st.button(f"🔄 أعد فحص الصفحات اللي فشل الوصول ليها ({len(failed_urls)}) — بطيء بس يوصل"):
                    recheck = []
                    prog = st.progress(0)
                    with ThreadPoolExecutor(max_workers=workers) as ex:
                        futs = [ex.submit(check_page, u) for u in failed_urls]
                        for i, f in enumerate(as_completed(futs), 1):
                            recheck.append(f.result())
                            prog.progress(int(i * 100 / len(futs)))
                    prog.empty()
                    by_url = {r["url"]: r for r in recheck}
                    ok_list  = [r for r in st.session_state["ok_pages"] if r["url"] not in by_url]
                    bad_list = [r for r in st.session_state["bad_pages"] if r["url"] not in by_url]
                    for r in recheck:
                        (bad_list if r["issues"] else ok_list).append(r)
                    st.session_state["ok_pages"]  = sorted(ok_list, key=lambda x: x["url"])
                    st.session_state["bad_pages"] = sorted(bad_list, key=lambda x: x["url"])
                    st.rerun()
        else:
            st.balloons()
            st.success("مبروك! كل الصفحات سليمة 🎉")
