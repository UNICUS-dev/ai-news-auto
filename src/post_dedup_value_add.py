# -*- coding: utf-8 -*-
import os, re, json, time, yaml, feedparser, math, hashlib
from pathlib import Path
from urllib.parse import urlparse, urljoin
from langdetect import detect, DetectorFactory
from dotenv import dotenv_values
from anthropic import Anthropic
import requests
from model_helper import create_message_with_fallback
from fact_checker import fact_check_article, print_fact_check_result, llm_fact_check_article, print_llm_fact_check_result
from requests.auth import HTTPBasicAuth
from difflib import SequenceMatcher
from bs4 import BeautifulSoup

DetectorFactory.seed = 0


def fetch_article_content(url: str, timeout: int = 15) -> str:
    """
    元記事のURLから本文を取得する

    Args:
        url: 記事のURL
        timeout: タイムアウト秒数

    Returns:
        記事本文のテキスト（取得失敗時は空文字列）
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # 不要な要素を削除
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'noscript']):
            tag.decompose()

        # 記事本文を探す（一般的なセレクタを試す）
        article_selectors = [
            'article',
            '[role="main"]',
            '.article-body',
            '.article-content',
            '.post-content',
            '.entry-content',
            '.story-body',
            'main',
        ]

        content = None
        for selector in article_selectors:
            element = soup.select_one(selector)
            if element:
                content = element.get_text(separator='\n', strip=True)
                if len(content) > 200:  # 十分なコンテンツがあれば採用
                    break

        # セレクタで見つからない場合はbody全体から
        if not content or len(content) < 200:
            body = soup.find('body')
            if body:
                content = body.get_text(separator='\n', strip=True)

        if content:
            # 長すぎる場合は切り詰め（トークン節約）
            if len(content) > 8000:
                content = content[:8000] + "..."
            return content

        return ""
    except Exception as e:
        print(f"[警告] 元記事の取得に失敗: {e}")
        return ""
BASE = Path(__file__).resolve().parent.parent
CFG  = yaml.safe_load(open(BASE/"config"/"config.yaml","r",encoding="utf-8"))
ENV  = dotenv_values(BASE/".env")
STATE_DIR = BASE/"state"; STATE_DIR.mkdir(exist_ok=True)

POSTED_URLS_PATH = STATE_DIR/"posted_urls.json"
DOMAIN_PATH      = STATE_DIR/"domain_last.json"
FINGER_PATH      = STATE_DIR/"posted_fingerprints.json"
IMG_HISTORY_PATH = STATE_DIR/"featured_image_history.json"
DOMAIN_HISTORY_PATH = STATE_DIR/"domain_history.json"  # 直近の投稿ドメイン履歴（多様性制御）

def load_json(p):
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except: return {}
    return {}

def save_json(p,d): p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf-8")

def load_posted_urls():
    s=set()
    if POSTED_URLS_PATH.exists():
        try:
            for u in json.loads(POSTED_URLS_PATH.read_text(encoding="utf-8")):
                s.add(norm_url(u))
        except: pass
    old = STATE_DIR/"posted.json"
    if old.exists():
        try:
            d=json.loads(old.read_text(encoding="utf-8"))
            for u in d.keys(): s.add(norm_url(u))
        except: pass
    return s

def save_posted_urls(s:set):
    POSTED_URLS_PATH.write_text(json.dumps(sorted(s),ensure_ascii=False,indent=2),encoding="utf-8")

def strip_html(s): return re.sub(r"<[^>]+>","", s or "").strip()

def norm_url(u:str)->str:
    u=(u or "").strip()
    u=re.sub(r"#.*$","",u); u=re.sub(r"/+$","",u)
    return u

def guess_lang(t):
    t=(t or "").strip()
    if not t: return "unknown"
    try: return detect(t)
    except: return "unknown"

def domain_ok(domain, domain_last, cooldown_days):
    ts=domain_last.get(domain); 
    return True if not ts else (time.time()-ts) > cooldown_days*86400

def mark_domain(domain, domain_last):
    if domain: domain_last[domain]=time.time()

def load_domain_history():
    d=load_json(DOMAIN_HISTORY_PATH)
    return d.get("items", []) if isinstance(d, dict) else []

def append_domain_history(domain):
    items=load_domain_history()
    items.append({"domain":domain, "ts":time.time()})
    if len(items)>400: items=items[-400:]
    save_json(DOMAIN_HISTORY_PATH, {"items":items})

def domain_post_count(domain, history, window_days):
    cutoff=time.time()-window_days*86400
    return sum(1 for it in history if it.get("domain")==domain and it.get("ts",0)>=cutoff)

import re as _re_nn
_NON_NEWS_DEFAULT = [
    r"\d+\s*選",          # 「8選」「12選」等のまとめ
    r"おすすめ",
    r"とは[?？。・…\s（(]",   # 「〜とは？」等の定義記事
    r"まとめ",
    r"完全ガイド",
    r"徹底解説",
    r"総まとめ",
    r"何ができる",
    r"できること",
    r"比較\s*\d",
]
def is_non_news(title, patterns=None):
    """常設まとめ/エバーグリーンSEO記事のタイトルパターンを検出"""
    pats = patterns if patterns else _NON_NEWS_DEFAULT
    t = title or ""
    for pat in pats:
        try:
            if _re_nn.search(pat, t):
                return True
        except _re_nn.error:
            if pat in t:
                return True
    return False

def clean_for_fingerprint(text:str)->str:
    t=strip_html(text)
    t=t.lower()
    t=re.sub(r"https?://\S+","",t)
    t=re.sub(r"[^ぁ-んァ-ン一-龥a-z0-9\s]", " ", t)
    t=re.sub(r"\s+"," ",t).strip()
    return t

def shingles(text, k=8):
    toks=text.split()
    return [" ".join(toks[i:i+k]) for i in range(max(1,len(toks)-k+1))]

def simhash(text, bits=64):
    v=[0]*bits
    for sh in shingles(text, k=8):
        h=int(hashlib.md5(sh.encode("utf-8")).hexdigest(),16)
        for i in range(bits):
            v[i]+=1 if (h>>i)&1 else -1
    out=0
    for i in range(bits):
        if v[i]>0: out|=(1<<i)
    return out

def hamdist(a,b):
    x=a^b; c=0
    while x: 
        x&=x-1; c+=1
    return c

def fingerprint_record(title:str, summary:str):
    base=clean_for_fingerprint((title or "")+" "+(summary or ""))
    if not base: base="(empty)"
    return {
        "sha1": hashlib.sha1(base.encode("utf-8")).hexdigest(),
        "simhash": simhash(base),
        "title": title[:120],
        "created_at": time.time()
    }

def select_featured_image():
    """ランダムに画像を選択（連続3回同じ画像を避ける）"""
    import random
    
    # 設定から画像IDリストを取得
    media_ids = CFG.get("wordpress", {}).get("featured_image", {}).get("random_media_ids", [])
    if not media_ids:
        return None
    
    # 履歴ファイルを読み込み
    history = load_json(IMG_HISTORY_PATH)
    recent = history.get("recent_images", [])
    
    # 連続3回同じ画像を避けるため、最近使った2つを除外
    available = [img_id for img_id in media_ids if img_id not in recent[-2:]]
    
    # 利用可能な画像がない場合は全てから選択
    if not available:
        available = media_ids
    
    # ランダム選択
    selected = random.choice(available)
    
    # 履歴更新（最新3件まで保持）
    recent.append(selected)
    recent = recent[-3:]  # 最新3件のみ保持
    
    # 履歴保存
    history["recent_images"] = recent
    history["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(IMG_HISTORY_PATH, history)
    
    return selected

def is_near_duplicate(title:str, summary:str, fp_list:list, sha1_dup=True, simhash_thresh=3, title_sim=0.92):
    base=clean_for_fingerprint((title or "")+" "+(summary or ""))
    if not base: return False
    sha1=hashlib.sha1(base.encode("utf-8")).hexdigest()
    sh=simhash(base)
    for r in fp_list:
        try:
            if sha1_dup and r.get("sha1")==sha1: 
                return True
            if "simhash" in r and hamdist(int(r["simhash"]), sh) <= simhash_thresh:
                return True
            t=r.get("title") or ""
            if t and SequenceMatcher(None, t.lower(), (title or "").lower()).ratio() >= title_sim:
                return True
        except: 
            continue
    return False

def safe_html_cleanup(html):
    html=re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html=re.sub(r"</?(script|style|section|table|iframe|form|noscript)\b[^>]*>.*?</\1>", "", html, flags=re.I|re.S)
    html=re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", html, flags=re.I|re.S)
    allowed=("h1","h3","p","ul","li","div","strong","em","code")
    def keep(m):
        tag=m.group(1).lower()
        return m.group(0) if tag in allowed else ""
    html=re.sub(r"</?([a-zA-Z0-9]+)\b[^>]*>", lambda m: keep(m), html)
    def noattrs(m):
        tag=m.group(1); closing=m.group(0).startswith("</")
        return f"</{tag}>" if closing else f"<{tag}>"
    html=re.sub(r"</?([a-zA-Z0-9]+)(\s+[^>]*)?>", noattrs, html)
    if len(html)>12000: html=html[:12000]+"…"
    return html

def generate_slug(title: str, client) -> str:
    try:
        msg = create_message_with_fallback(
            client,
            system="URLスラッグ生成器。英語スラッグのみを返す。",
            messages=[{
                "role": "user",
                "content": (
                    f"以下の日本語記事タイトルを、SEOに適した英語のURLスラッグに変換してください。\n"
                    f"ルール: 小文字・ハイフン区切り・3〜6単語・英数字とハイフンのみ・記事の主要キーワードを含める\n"
                    f"スラッグのみを返してください。説明不要。\n\n"
                    f"タイトル: {title}"
                )
            }],
            max_tokens=50,
            temperature=0.0
        )
        slug = "".join([p.text for p in msg.content if p.type == "text"]).strip().lower()
        slug = re.sub(r'[^a-z0-9-]', '-', slug)
        slug = re.sub(r'-+', '-', slug).strip('-')
        return slug if slug else "ai-news"
    except Exception as e:
        print(f"[警告] スラッグ生成に失敗: {e}")
        return "ai-news"

COURSES = [
    ("https://unicus.top/gas_automation_seminor/",    "GASで始める超速・自動化入門（Eラーニング講座）",  ["自動化","GAS","Apps Script","スクリプト","業務効率","RPA","ワークフロー","スプレッドシート","効率化"]),
    ("https://unicus.top/rag_seminor/",               "NotebookLMで始めるAI業務改革（Eラーニング講座）", ["RAG","NotebookLM","社内資料","ナレッジ","知識","検索拡張","ドキュメント"]),
    ("https://unicus.top/prompt_engineering_seminor/","プロンプトエンジニアリング研修（Eラーニング講座）",  ["プロンプト","ChatGPT","Claude","生成AI","文章生成","画像生成","LLM","対話"]),
    ("https://unicus.top/think_prompt_seminar/",      "思考×プロンプト基礎研修（Eラーニング講座）",      ["思考","論理","フレームワーク","問題解決"]),
]

def _pick_course(haystack):
    best=None; best_score=0
    for url, label, kws in COURSES:
        score=sum(haystack.count(k) for k in kws)
        if score>best_score:
            best_score=score; best=(url,label)
    return best if best_score>=2 else None

def _first_post(api, auth, params, used, exclude_slug):
    try:
        rr=requests.get(api, params=params, auth=auth, timeout=20)
        for it in (rr.json() if rr.status_code==200 else []):
            if it.get("slug")==exclude_slug:
                continue
            t=re.sub(r"<[^>]+>","",(it.get("title",{}) or {}).get("rendered","")).strip()
            l=it.get("link","")
            if t and l and l not in used:
                used.add(l)
                return (l,t)
    except Exception:
        pass
    return None

def build_related_links_block(wp_url, wp_user, wp_pass, cats, haystack="", exclude_slug=""):
    """内部リンク3本(新着 / 同カテゴリ / Eラーニング講座)のブロックを生成。
    講座は本文内容にキーワード一致した場合のみ。一致が弱ければ別の関連記事で代替。"""
    try:
        api=urljoin(wp_url, "wp-json/wp/v2/posts")
        auth=HTTPBasicAuth(wp_user, wp_pass)
        cat_param=",".join(str(c) for c in cats) if cats else None
        used=set(); links=[]
        # 1) 新着（全体で最新）
        r1=_first_post(api, auth, {"per_page":3,"orderby":"date","status":"publish","_fields":"title,link,slug"}, used, exclude_slug)
        if r1: links.append(r1)
        # 2) 同カテゴリ（最新）
        if cat_param:
            r2=_first_post(api, auth, {"categories":cat_param,"per_page":4,"orderby":"date","status":"publish","_fields":"title,link,slug"}, used, exclude_slug)
            if r2: links.append(r2)
        # 3) Eラーニング講座（内容一致）／弱ければ関連記事で代替
        course=_pick_course(haystack)
        if course:
            links.append(course)
        else:
            params={"per_page":6,"orderby":"date","status":"publish","_fields":"title,link,slug"}
            if cat_param: params["categories"]=cat_param
            r3=_first_post(api, auth, params, used, exclude_slug)
            if r3: links.append(r3)
        if not links:
            return ""
        lis="".join('<li><a href="%s">%s</a></li>' % (l, t) for l, t in links)
        return '\n<h3>関連記事・おすすめ講座</h3>\n<ul class="unicus-related">' + lis + '</ul>\n'
    except Exception as e:
        print("[警告] 関連リンク生成に失敗: %s" % e)
        return ""

def generate_focus_keyphrase(title, summary, client):
    """SEO用フォーカスキーフレーズをLLMで生成"""
    try:
        msg = create_message_with_fallback(
            client,
            system="SEOのフォーカスキーフレーズ生成器。日本語で2〜4語の簡潔なキーフレーズのみを返す。記号や説明は不要。",
            messages=[{"role": "user", "content": "次の記事の主題を表すSEOフォーカスキーフレーズを1つだけ返してください。\nタイトル: %s\n要約: %s" % (title, (summary or "")[:200])}],
            max_tokens=30,
            temperature=0.0,
        )
        kw = "".join([c.text for c in msg.content if c.type == "text"]).strip()
        kw = re.sub(r"\s+", " ", kw).strip("「」\"'　 ")
        return kw[:60]
    except Exception as e:
        print("[警告] キーフレーズ生成に失敗: %s" % e)
        return ""

def entry_published_ts(e):
    try:
        from time import mktime
        if getattr(e,"published_parsed",None): return mktime(e.published_parsed)
        if getattr(e,"updated_parsed",None): return mktime(e.updated_parsed)
    except: pass
    return None

def determine_article_type(c, client):
    """
    記事のタイプを判定する（Type A, B, C）

    Type A: 実践的なノウハウ・ハウツーが書ける内容
    Type B: ハウツー記事を紹介できる内容
    Type C: ニュースまとめとして書く内容

    Returns:
        str: "howto_creation", "howto_introduction", "news_summary"
    """
    prompt = f"""以下のニュースを読んで、どのタイプの記事が最適か判定してください。

タイトル: {c["title"]}
要約: {c["summary"]}

【判定基準】
Type A (howto_creation):
- この情報をもとに、読者が実践できる具体的なノウハウやハウツーを書ける
- 新機能の使い方、設定方法、実装手順などを解説できる
- 具体的な「やり方」を教えられる内容

Type B (howto_introduction):
- この記事の元ネタ自体が、ハウツーやノウハウを含んでいる
- その記事を紹介・要約することで読者に価値を提供できる
- 元記事のチュートリアルやガイドを日本語で紹介する形

Type C (news_summary):
- 発表、ニュース、レポートなどの速報的な内容
- 「何が起きたか」を伝えるのが主目的
- 実践的なノウハウよりも情報の伝達が重要

以下のいずれかのみを返答してください（説明不要）:
- howto_creation
- howto_introduction
- news_summary"""

    try:
        msg = create_message_with_fallback(
            client,
            system="記事タイプ判定器。指定された3つのタイプのいずれかのみを返す。",
            messages=[{"role":"user","content":prompt}],
            max_tokens=20,
            temperature=0.0
        )
        txt = "".join([p.text for p in msg.content if p.type=="text"]).strip().lower()

        if "howto_creation" in txt:
            return "howto_creation"
        elif "howto_introduction" in txt:
            return "howto_introduction"
        else:
            return "news_summary"
    except Exception as e:
        print(f"[警告] 記事タイプ判定に失敗: {e}")
        return "news_summary"  # デフォルトはニュースまとめ

def score_candidate(c, sel, client):
    W = sel["weights"]
    now = time.time()
    freshness=0.0
    if c.get("ts"):
        hours=max(1,(now-c["ts"])/3600.0)
        freshness=max(0.0,min(1.0, math.exp(-hours/72.0)))
    lang_score = sel.get("ja_priority",1.0) if c["lang"].startswith("ja") else sel.get("en_priority",0.8)
    src_w = sel.get("source_weights",{}).get(c["domain"], 1.0)
    kw_score = 0.0
    title_lower=c["title"]
    for kw in sel.get("keyword_boosts",[]):
        if kw.lower() in title_lower.lower():
            kw_score += 0.05
    kw_score=min(1.0, kw_score)
    prompt = f"""次のニュースが、LLM/生成AI領域で日本のビジネス読者にとって「話題になる/価値が高い」かを0.0〜1.0で数値のみ返答。
特に公式発表・カンファレンス・DevDay・API更新・新機能リリースは高評価。説明不要。
タイトル: {c["title"]}
要約: {c["summary"]}"""
    try:
        msg = create_message_with_fallback(
            client,
            system="数値評価器。0.0〜1.0の実数のみを返す。",
            messages=[{"role":"user","content":prompt}],
            max_tokens=20,
            temperature=0.0
        )
        txt="".join([p.text for p in msg.content if p.type=="text"]).strip()
        m=re.findall(r"[0-1](?:\.\d+)?", txt)
        vir=float(m[0]) if m else 0.5
    except Exception:
        vir=0.5

    # 記事タイプを判定してボーナスを追加
    article_type = determine_article_type(c, client)
    c["article_type"] = article_type  # 後で使用するために保存

    # config.yamlから記事タイプボーナスを取得
    type_bonuses = CFG.get("generate", {}).get("content_strategy", {}).get("article_type_priority", {})
    type_bonus = type_bonuses.get(article_type, 0.0)

    score = (W["freshness"]*freshness +
             W["source"]*( (src_w-0.8)/0.4*0.5 ) +
             W["language"]*lang_score +
             W["keyword"]*kw_score +
             W["llm_virality"]*vir +
             type_bonus)  # 記事タイプボーナスを追加
    return max(0.0, min(1.0, score))

def pick_candidates(top_n=5):
    """
    記事候補を取得し、スコアの高い順にtop_n件を返す

    Returns:
        candidates: 候補記事のリスト（スコア順）
        posted_urls: 投稿済みURLセット
        domain_last: ドメイン最終投稿時刻
        fp_list: フィンガープリントリスト
    """
    sel=CFG.get("selection",{})
    feeds=CFG.get("fetch",{}).get("feeds",[])
    posted_urls=load_posted_urls()
    domain_last=load_json(DOMAIN_PATH)
    fp_list=load_json(FINGER_PATH).get("items",[])
    cand_limit=sel.get("candidate_limit",50)
    scan_per_feed=sel.get("max_scan_per_feed",10)
    cooldown=sel.get("domain_cooldown_days",1)
    div=sel.get("source_diversity",{}) or {}
    div_window=div.get("window_days",30)
    div_max=div.get("max_per_domain",2)
    div_penalty=div.get("penalty_per_post",0.15)
    domain_history=load_domain_history()
    max_age_days=sel.get("max_article_age_days")          # これより古い記事は除外
    max_per_run=sel.get("max_per_domain_per_run", 2)       # 1実行内の同一ドメイン上限
    non_news_patterns=sel.get("non_news_title_patterns")   # 未指定なら既定パターン
    per_run_domain={}
    excluded_keywords=sel.get("excluded_keywords",[])
    client=Anthropic(api_key=ENV.get("ANTHROPIC_API_KEY"))
    cands=[]
    for f in feeds:
        url=f.get("url");
        if not url: continue
        d=feedparser.parse(url)
        for e in d.entries[:scan_per_feed]:
            title=strip_html(getattr(e,"title",""))
            link=(getattr(e,"link","") or "").strip()
            if not link: continue
            nlink=norm_url(link)
            if nlink in posted_urls:
                continue
            summary=strip_html(getattr(e,"summary","") or getattr(e,"description",""))

            # セール・商業記事の除外チェック
            text_to_check = (title + " " + summary).lower()
            is_excluded = False
            for keyword in excluded_keywords:
                if keyword.lower() in text_to_check:
                    is_excluded = True
                    break
            if is_excluded:
                continue

            # ニュースでない常設まとめ/エバーグリーン記事を除外
            if is_non_news(title, non_news_patterns):
                continue

            if is_near_duplicate(title, summary, fp_list, sha1_dup=True, simhash_thresh=3, title_sim=0.92):
                continue
            dom=urlparse(link).netloc
            if not domain_ok(dom, domain_last, cooldown):
                continue
            if div_max is not None and domain_post_count(dom, domain_history, div_window) >= div_max:
                continue
            ts=entry_published_ts(e)
            # 時事性の足切り: 公開日が分かる かつ 古すぎる場合は除外
            if max_age_days and ts and (time.time()-ts) > max_age_days*86400:
                continue
            # 1実行内の同一ドメイン候補を上限まで
            if per_run_domain.get(dom,0) >= max_per_run:
                continue
            per_run_domain[dom]=per_run_domain.get(dom,0)+1
            lang=guess_lang((title+" "+summary)[:1000])
            cands.append({"title":title,"link":link,"summary":summary,"domain":dom,"ts":ts,"lang":lang,"source":d.feed.get("title",url)})
            if len(cands)>=cand_limit: break
        if len(cands)>=cand_limit: break
    if not cands: return [], posted_urls, domain_last, fp_list
    scored=[]
    for c in cands:
        s=score_candidate(c, sel, client)
        recent_n=domain_post_count(c["domain"], domain_history, div_window)
        s=max(0.0, s - div_penalty*recent_n)
        scored.append((s,c))
    scored.sort(key=lambda x: x[0], reverse=True)
    # 上位top_n件を返す
    top_candidates = [item[1] for item in scored[:top_n]]
    return top_candidates, posted_urls, domain_last, fp_list

def create_article_prompt(article_type, best, article_content):
    """
    記事タイプに応じたプロンプトを生成する

    Args:
        article_type: "howto_creation", "howto_introduction", "news_summary"
        best: 候補記事の情報
        article_content: 元記事の本文

    Returns:
        str: 生成用プロンプト
    """
    # 共通のFAQセクション
    faq_section = """
5. FAQ（必須）
<h3>よくある質問</h3>

<p><strong>Q1. （質問文）</strong></p>
<p>A1. （回答文: Yes/Noまたは結論から開始、80-150文字）</p>

<p><strong>Q2. （質問文）</strong></p>
<p>A2. （回答文）</p>

<p><strong>Q3. （質問文）</strong></p>
<p>A3. （回答文）</p>

【FAQの作成ルール】
- 実際にこの記事を読んだ人が次に抱く疑問を質問にすること
- 質問は記事本文に書かれていない内容か、より深く知りたい内容であること
- 回答の冒頭は必ず「Yes/No」または結論から始めること
- 回答は80〜150文字に収めること
- 専門用語が出た場合はカッコ内で簡単に説明を加えること

6. 出典
<div class="source"><strong>出典：</strong>【元記事タイトル】（【ドメイン】）</div>"""

    article_content_section = f"""
元記事情報：
- タイトル: {best['title']}
- リンク: {best['link']}
- 要約: {best['summary']}
- ドメイン: {best['domain']}
- 言語: {best['lang']}
{f'''
元記事本文：
{article_content}
''' if article_content else '（元記事本文の取得に失敗したため、要約のみを参照してください）'}

HTMLのみで出力してください。Markdown禁止。コードブロックマーカーは使用禁止。"""

    if article_type == "howto_creation":
        # Type A: 実践的なハウツー記事作成
        return f"""以下の情報をもとに、読者が実践できる具体的なハウツー記事を作成してください。

【記事構成】

1. メタディスクリプション（120字以内）
<p data-meta="description">【何ができるか】【どのように使うか】【メリット1文】</p>

2. タイトル（H1、50-60文字）
<h1>【実践できる内容を明示】</h1>

3. リード段落（300-400字）
<p>
【第1文】何ができるようになったのか
【第2-3文】この機能・手法の概要と特徴
【第4-5文】誰が使うべきか、どんな場面で役立つか
【第6文】この記事で学べること
</p>

4. 本文（H3見出しで整理、全体で2,500〜3,000字）

<h3>【機能・手法の概要】</h3>
<p>
・どんな機能・手法なのか
・従来の方法との違い
・利用可能な条件（APIアクセス、必要なツールなど）
</p>

<h3>具体的な使い方・実装手順</h3>
<p>
【ステップ1】【具体的な手順】
【ステップ2】【具体的な手順】
【ステップ3】【具体的な手順】

各ステップで注意すべきポイントや、設定項目の説明を含める。
専門用語は「○○とは、～のことです」の形で説明。
</p>

<h3>実践のコツと注意点</h3>
<p>
・うまく使うためのヒント
・よくある失敗と回避方法
・パフォーマンスやコストの考慮事項
</p>

<h3>活用事例とユースケース</h3>
<p>
【具体例1】【どんな場面で】【どう使うか】
【具体例2】【どんな場面で】【どう使うか】

実際にどのような場面で役立つかを具体的に。
</p>

{faq_section}

【重要な指示】
- 読者が実際に試せる具体的な手順を含めること
- 抽象的な説明ではなく、実践的なノウハウを提供すること
- 専門用語には必ず説明と具体例をつけること
- 手順は明確で分かりやすく
- 【最重要】各ステップは「何をするか」だけでなく「どうやるか（具体的な操作・設定・判断基準・数値）」まで書く。一文で済ませない。
- 元記事に複数の工程・事例がある場合は、それぞれを独立したH3で省略せず展開する。
- 本文全体で3,000字程度を目安に、濃く解説する。

{article_content_section}"""

    elif article_type == "howto_introduction":
        # Type B: ハウツー記事紹介
        return f"""以下の元記事には実践的なハウツーやノウハウが含まれています。その内容を日本語で分かりやすく紹介する記事を作成してください。

【記事構成】

1. メタディスクリプション（120字以内）
<p data-meta="description">【どんなハウツーか】【誰向けか】【主な内容1文】</p>

2. タイトル（H1、50-60文字）
<h1>【紹介するハウツーの内容を明示】</h1>

3. リード段落（300-400字）
<p>
【第1文】どんなハウツー・ガイドが公開されたか
【第2-3文】誰が公開し、何を目的としているか
【第4-5文】このガイドで学べる主な内容
【第6文】どんな人に役立つか
</p>

4. 本文（H3見出しで整理、全体で2,500〜3,000字）

<h3>このガイドについて</h3>
<p>
・誰が公開したガイドか
・どんな目的・背景で作られたか
・どのレベルの読者を対象としているか
</p>

<h3>【核心トピック1：元記事の主要な手法・工程を具体的な見出しに】</h3>
<p>
元記事で説明されている「やり方」そのものを、ここで丁寧に展開します。
・具体的に何を、どの順序で、どうやるのか（手順・設定・判断基準）を書く
・なぜそうするのか（理由・背景）も添える
・数値、期間、ツール名、担当者の発言など、元記事にある具体情報を必ず盛り込む
・一文で流さず、読者がその手法を理解・再現できるレベルまで説明する
</p>

<h3>【核心トピック2：次の重要な手法・工程・事例】</h3>
<p>
2つ目の重要ポイントを、同じ深さで具体的に解説します。中身（どうやるのか）を省略しないこと。
</p>

<h3>【核心トピック3以降：列挙がある場合は1項目ずつ独立見出しで】</h3>
<p>
元記事に「6工程」「5ステップ」「3つの教訓」などの列挙がある場合は、
それぞれを必ず1つずつ独立したH3見出しで取り上げ、各項目の実際の中身（具体的な実行方法）を
省略せずに解説します。要点だけを並べて中身を省くことは禁止です。
</p>

<h3>注目すべきポイント</h3>
<p>
このガイドで特に参考になる部分：
・【ポイント1】
・【ポイント2】
・【ポイント3】

実務で活用できるヒントや、見落としがちな重要事項。
</p>

<h3>こんな人におすすめ</h3>
<p>
・【対象者1】【理由】
・【対象者2】【理由】
・【対象者3】【理由】

このガイドを読むべき人と、得られるメリット。
</p>

{faq_section}

【重要な指示】
- 元記事のハウツー内容を正確に紹介すること
- 読者がこのガイドから何を学べるかを明確にすること
- 元記事へのリンクを出典として必ず記載すること
- 専門用語には必ず説明をつけること
- 【最重要】元記事で最も価値のある「具体的なやり方・手順・各工程の中身」は、絶対に要約・省略しないこと。ここに最も多くの字数を割く。
- 「〜する方法を説明しています」「〜の重要性を強調しています」のように見出しだけ述べて中身を書かないのは禁止。必ずその"中身"（どうやるのか）を展開する。
- 元記事が複数の工程・ステップ・事例を列挙している場合、それぞれを独立したH3見出しで1つずつ詳しく解説する。
- 本文全体で3,000字程度を目安に、薄いまとめではなく濃い解説にする。

{article_content_section}"""

    else:  # news_summary
        # Type C: ニュースまとめ（現行と同じ）
        return f"""以下の元記事から、わかりやすい日本語ニュース記事を作成してください。

【記事構成】

1. メタディスクリプション（120字以内）
<p data-meta="description">【誰が】【何を】【いつ】。【主な内容1文】。【影響1文】。</p>

2. タイトル（H1、50-60文字）
<h1>【事実を簡潔に】</h1>

3. リード段落（300-400字）※重要：丁寧にわかりやすく説明
<p>
【第1文】いつ、誰が、何をしたか（5W1H）
【第2-3文】その内容の詳細、重要なポイント
【第4-5文】なぜこれが重要なのか、背景
【第6文】読者への影響や意味
</p>

4. 本文（H3見出しで整理、全体で2,500〜3,000字）

<h3>【具体的な見出し】</h3>
<p>
・発表内容の詳細
・数値やデータがあれば具体的に
・専門用語は「○○とは、～のことです。【具体例】」の形で説明
</p>

<h3>背景と経緯</h3>
<p>
・なぜこのニュースが出たのか
・これまでの経緯
・業界の状況
</p>

<h3>技術的な詳細（該当する場合）</h3>
<p>
・どんな技術・仕組みか
・従来との違い
・具体的にどう動作するか
※難しい概念は身近な例えで説明
</p>

<h3>できること・できないこと</h3>
<p>
【文章形式で説明】
この技術により、【具体的にできること】が可能になります。例えば、【具体例1】や【具体例2】といった使い方が考えられます。

一方で、【まだ難しいこと】もあります。【制約や限界を説明】。【時期】には【改善の見込み】でしょう。
</p>

<h3>私たちへの影響</h3>
<p>
【読者の視点で】
このニュースは、【対象読者】に【どんな影響】を与えます。

【短期的な影響】については、【具体的に】。
【中長期的な影響】としては、【予測】が考えられます。

ただし、【注意点や留意事項】。
</p>

{faq_section}

【重要な指示】
- リード段落は特に丁寧に、300-400字かけて説明する
- 逆ピラミッド構造：重要な情報を最初に
- 箇条書きは必要最小限、基本は文章で説明
- 「できること・できないこと」「影響」は文章形式
- 専門用語には必ず説明と具体例をつける
- 【最重要】元記事の核心（具体的な手法・手順・データ・各論点の中身）は要約で流さず、それぞれを十分な字数で展開する。見出しだけ立てて中身を省略しない。
- 元記事が複数の論点・工程・事例を挙げている場合は、各々を独立したH3で具体的に解説する。
- 本文全体で2,500〜3,000字を目安に、薄いまとめにしない。

{article_content_section}"""

def main():
    WP_URL=(ENV.get("WP_URL","") or "").rstrip("/")+"/"
    WP_USER=(ENV.get("WP_USER","") or "")
    WP_PASS=(ENV.get("WP_APP_PASSWORD","") or "")
    if not (WP_URL and WP_USER and WP_PASS):
        raise SystemExit("WP接続情報不足")
    wp_cfg=(CFG.get("wordpress") or {})
    cats=wp_cfg.get("category_ids") or []
    status=wp_cfg.get("status","publish")

    # 複数の候補を取得（上位5件）
    candidates, posted_urls, domain_last, fp_list = pick_candidates(top_n=5)
    if not candidates:
        print("未投稿の候補が見つかりません。終了。"); return

    print(f"\n{len(candidates)}件の候補記事を取得しました。")

    client=Anthropic(api_key=ENV.get("ANTHROPIC_API_KEY"))
    system="""あなたは技術ニュースライターです。

【記事作成の原則】
- ニュース記事として事実を正確に伝える
- 専門知識のない読者でも理解できるよう丁寧に説明する
- 読者にとっての意味や影響を明示する

【文章ルール】
- 1文は20語以内を目安に、短く簡潔に
- 1段落は3-5文以内
- 専門用語は初出時に必ず説明（「○○とは、～のことです」）
- 具体的な数値・日付・固有名詞を使う
- 曖昧な表現を避ける

【HTMLタグ】
- h1, h3, p, ul, li, div, strong, em, codeのみ使用
- コードブロックマーカー（```html など）は絶対に出力しない
- HTMLタグの外にテキストを書かない"""

    # 候補を順に試す
    for idx, best in enumerate(candidates, 1):
        print(f"\n{'='*70}")
        print(f"候補 {idx}/{len(candidates)}: {best['title'][:60]}...")
        print(f"{'='*70}")

        # 元記事の本文を取得
        print("\n[元記事を取得中...]")
        article_content = fetch_article_content(best['link'])
        if article_content:
            print(f"✅ 元記事を取得しました（{len(article_content)}文字）")
        else:
            print("⚠️ 元記事の取得に失敗。RSS要約のみで生成します。")

        # 記事タイプに応じたプロンプトを生成
        article_type = best.get('article_type', 'news_summary')
        type_labels = {
            'howto_creation': 'Type A (実践的ハウツー作成)',
            'howto_introduction': 'Type B (ハウツー記事紹介)',
            'news_summary': 'Type C (ニュースまとめ)'
        }
        print(f"\n📝 記事タイプ: {type_labels.get(article_type, article_type)}")

        user = create_article_prompt(article_type, best, article_content)

        print("\n[記事生成中...]")
        msg=create_message_with_fallback(client, system=system, messages=[{"role":"user","content":user}])
        html="".join([p.text for p in msg.content if p.type=="text"]).strip()

        if not html:
            print("❌ 生成が空。次の候補へ。")
            continue

        # Phase 1: ルールベースのファクトチェック
        print("\n[Phase 1: ルールベースのファクトチェック中...]")
        fact_check_result = fact_check_article(best, html)
        print_fact_check_result(fact_check_result)

        if not fact_check_result["passed"]:
            print(f"❌ Phase 1 不合格。この記事を破棄して次の候補へ。\n")
            continue

        print("✅ Phase 1 合格！")

        # Phase 2: LLMベースのファクトチェック
        print("\n[Phase 2: LLMベースのファクトチェック中...]")
        llm_result = llm_fact_check_article(best, html, client)
        print_llm_fact_check_result(llm_result)

        if not llm_result["passed"]:
            print(f"❌ Phase 2 不合格（スコア: {llm_result['score']}/100）。この記事を破棄して次の候補へ。\n")
            continue

        # 両方のファクトチェック合格 → 投稿処理
        print(f"✅ Phase 2 合格（スコア: {llm_result['score']}/100）！")
        print("✅ 全てのファクトチェックに合格！記事を投稿します。\n")

        meta=""
        m=re.search(r'<p[^>]*data-meta=["\\\']description["\\\'][^>]*>(.*?)</p>', html, flags=re.I|re.S)
        if m:
            meta=re.sub(r"<[^>]+>","",m.group(1)).strip()
            if len(meta)>120: meta=meta[:119]+"…"
        html=safe_html_cleanup(html)
        mt=re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.I|re.S)
        title=re.sub(r"<[^>]+>","",mt.group(1)).strip()[:62] if mt else "(自動生成)AIニュース"

        slug = generate_slug(title, client)
        print(f"スラッグ: {slug}")

        # ① 関連記事の内部リンクを本文末尾に付与（safe_html_cleanup後なので<a>が残る）
        related_block = build_related_links_block(WP_URL, WP_USER, WP_PASS, cats, haystack=(title + " " + re.sub(r"<[^>]+>", "", html)), exclude_slug=slug)
        if related_block:
            html = html + related_block
            print("関連記事リンクを追加しました")

        # ② Yoast フォーカスキーフレーズを生成
        focuskw = generate_focus_keyphrase(title, best.get("summary", ""), client)
        print("フォーカスキーフレーズ: %s" % focuskw)

        url=urljoin(WP_URL,"wp-json/wp/v2/posts")
        payload={"title":title,"content":html,"status":status,"categories":cats,"excerpt":meta,"slug":slug,
                 "meta":{"_yoast_wpseo_metadesc": meta, "_yoast_wpseo_focuskw": focuskw}}

        # アイキャッチ画像をランダム選択
        featured_img_id = select_featured_image()
        if featured_img_id:
            payload["featured_media"] = featured_img_id
            print(f"アイキャッチ画像: ID {featured_img_id}")

        r=requests.post(url,auth=HTTPBasicAuth(WP_USER,WP_PASS),json=payload,timeout=40)
        print("POST STATUS:", r.status_code)
        try:
            data=r.json()
            print(json.dumps({k:data.get(k) for k in["id","status","link","date","categories"]},ensure_ascii=False,indent=2))
            if r.status_code==201:
                posted_urls.add(norm_url(best["link"]))
                save_posted_urls(posted_urls)
                fp_list = load_json(FINGER_PATH).get("items",[])
                fp_list.append(fingerprint_record(best["title"], best["summary"]))
                if len(fp_list) > 2000:
                    fp_list = fp_list[-1000:]
                save_json(FINGER_PATH, {"items": fp_list})
                domain_last=load_json(DOMAIN_PATH); domain_last[best["domain"]] = time.time(); save_json(DOMAIN_PATH, domain_last)
                append_domain_history(best["domain"])  # 多様性制御用の投稿履歴に追加
                print("\n✅ 記事投稿成功！")
                return  # 成功したら終了
        except Exception as e:
            print(f"投稿エラー: {e}")
            print(r.text[:500])
            continue  # エラーの場合は次の候補へ

    print("\n❌ すべての候補記事がファクトチェックまたは投稿に失敗しました。")

if __name__=="__main__":
    main()
