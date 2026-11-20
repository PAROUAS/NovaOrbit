#!/usr/bin/env python3
"""NOVA local search application. Python standard library only."""
from __future__ import annotations

import html
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from wsgiref.util import setup_testing_defaults

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NOVA_DB", str(HERE / "data" / "nova.sqlite3")))
PORT = int(os.environ.get("NOVA_ONE_PORT", "5001"))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
db_lock = threading.RLock()
crawler_lock = threading.RLock()
crawler = {"running": False, "queued": 0, "crawled": 0, "errors": 0, "current": "", "started_at": None, "last_event": "Ready"}
robots_cache = {}
mail_app = None


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS searches(
          id INTEGER PRIMARY KEY, query TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Web',
          created_at TEXT NOT NULL, result_count INTEGER NOT NULL DEFAULT 0,
          latency_ms INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ok', clicked_url TEXT
        );
        CREATE INDEX IF NOT EXISTS searches_created ON searches(created_at);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents(
          id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL,
          crawled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'indexed'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(title, description, body, url UNINDEXED, domain UNINDEXED);
        CREATE TABLE IF NOT EXISTS crawler_jobs(
          id INTEGER PRIMARY KEY, seed TEXT NOT NULL, status TEXT NOT NULL,
          queued INTEGER NOT NULL DEFAULT 0, crawled INTEGER NOT NULL DEFAULT 0,
          errors INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, finished_at TEXT
        );
        """)
        fts=con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='document_search'").fetchone()
        if fts and "content=''" in (fts[0] or "").replace(" ",""):
            con.execute("DROP TABLE document_search")
            fts=None
        if not fts:
            con.execute("CREATE VIRTUAL TABLE document_search USING fts5(title, description, body, url UNINDEXED, domain UNINDEXED)")
            con.execute("INSERT INTO document_search(rowid,title,description,body,url,domain) SELECT id,title,description,body,url,domain FROM documents")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.item = None
        self.capture = None
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get("class", "").split()
        if tag == "div" and "result" in classes and "result--ad" not in classes:
            self.item = {"title": "", "url": "", "snippet": ""}
            self.items.append(self.item)
        if self.item is None:
            return
        if tag == "a" and "result__a" in classes:
            self.item["url"] = a.get("href", "")
            self.capture, self.buffer = "title", []
        elif any(x.startswith("result__snippet") for x in classes):
            self.capture, self.buffer = "snippet", []

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if self.capture == "title" and tag == "a":
            self.item["title"] = " ".join("".join(self.buffer).split())
            self.capture = None
        elif self.capture == "snippet" and tag in ("a", "div"):
            self.item["snippet"] = " ".join("".join(self.buffer).split())
            self.capture = None


def unwrap_url(url):
    p = urllib.parse.urlparse(html.unescape(url))
    if p.hostname and (p.hostname.endswith("duckduckgo.com") or p.hostname.endswith("duck.com")):
        target = urllib.parse.parse_qs(p.query).get("uddg", [""])[0]
        if target:
            return target
    return html.unescape(url)


def fetch_json(url, agent=USER_AGENT):
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=16) as response:
        return json.loads(response.read(2_500_000).decode("utf-8", "replace"))


def live_search(query, category="Web", page=1, safe=True):
    params = {"q": query, "s": str((page - 1) * 30), "kp": "1" if safe else "-2"}
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=18) as response:
        source = response.read(2_500_000).decode("utf-8", "replace")
    if "anomaly-modal" in source or "bots use DuckDuckGo" in source:
        raise RuntimeError("The search provider temporarily asked us to slow down. Please try again in a moment.")
    parser = ResultParser()
    parser.feed(source)
    results = []
    for row in parser.items:
        target = unwrap_url(row["url"])
        if not row["title"] or not target.startswith(("http://", "https://")):
            continue
        host = urllib.parse.urlparse(target).hostname or ""
        results.append({"title": row["title"], "url": target, "domain": host.removeprefix("www."), "snippet": row["snippet"], "source": "Live web"})
    # Remove duplicate targets while preserving the provider's relevance order.
    unique = []
    seen = set()
    for row in results:
        if row["url"] not in seen:
            unique.append(row)
            seen.add(row["url"])
    return unique


def news_search(query, page=1):
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    req = urllib.request.Request("https://news.google.com/rss/search?" + params, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=16) as response:
        root = ET.fromstring(response.read(1_500_000))
    items = root.findall("./channel/item")[(page - 1) * 20:page * 20]
    results=[]
    for item in items:
        title=item.findtext("title", ""); url=item.findtext("link", ""); desc=item.findtext("description", "")
        source=item.find("source"); publisher=source.text if source is not None else "Google News"
        if not title or not url: continue
        clean=html.unescape(re.sub(r"<[^>]+>", " ", desc)); clean=" ".join(clean.split())
        results.append({"title":title,"url":url,"domain":publisher,"snippet":clean,"source":"Live news","published":item.findtext("pubDate","")})
    return results


def image_search(query, page=1):
    params={"action":"query","generator":"search","gsrsearch":query,"gsrnamespace":"6","gsrlimit":"20","gsroffset":str((page-1)*20),"prop":"imageinfo","iiprop":"url|size","iiurlwidth":"520","format":"json"}
    data=fetch_json("https://commons.wikimedia.org/w/api.php?"+urllib.parse.urlencode(params),"NOVA local search/1.0 (Wikimedia Commons API)")
    pages=(data.get("query") or {}).get("pages",{})
    rows=[]
    for page_data in sorted(pages.values(),key=lambda x:x.get("index",0)):
        info=(page_data.get("imageinfo") or [{}])[0]; title=page_data.get("title","")
        thumb=info.get("thumburl") or info.get("url")
        if not thumb: continue
        rows.append({"title":title.removeprefix("File:"),"url":"https://commons.wikimedia.org/wiki/"+urllib.parse.quote(title.replace(" ","_")),"domain":"Wikimedia Commons","snippet":f"{info.get('width','?')} × {info.get('height','?')} pixels · Wikimedia Commons","source":"Live images","image":thumb,"width":info.get("width"),"height":info.get("height")})
    return rows


def video_search(query, page=1):
    params={"search":query,"fields":"id,title,url,thumbnail_360_url,duration,owner.screenname,views_total","limit":"20","page":str(page)}
    data=fetch_json("https://api.dailymotion.com/videos?"+urllib.parse.urlencode(params))
    results=[]
    for v in data.get("list",[]):
        seconds=int(v.get("duration") or 0)
        duration=f"{seconds//60}:{seconds%60:02d}"
        results.append({"title":v.get("title","Untitled video"),"url":v.get("url",""),"domain":v.get("owner.screenname") or "Dailymotion","snippet":f"{int(v.get('views_total') or 0):,} views · {duration}","source":"Live video","image":v.get("thumbnail_360_url",""),"duration":seconds})
    return results


def map_search(query):
    params={"format":"jsonv2","q":query,"limit":"15","addressdetails":"1"}
    data=fetch_json("https://nominatim.openstreetmap.org/search?"+urllib.parse.urlencode(params),"NOVA local search/1.0 (OpenStreetMap Nominatim)")
    results=[]
    for place in data:
        oid=place.get("osm_id"); typ=place.get("osm_type","node").lower()
        url=f"https://www.openstreetmap.org/{typ}/{oid}" if oid else "https://www.openstreetmap.org/search?query="+urllib.parse.quote(query)
        results.append({"title":place.get("name") or place.get("display_name","Map result").split(",")[0],"url":url,"domain":"OpenStreetMap","snippet":place.get("display_name",""),"source":"Live map","lat":float(place["lat"]),"lon":float(place["lon"])})
    return results


def category_search(query, category, page, safe):
    if category=="Images": return image_search(query,page),"Wikimedia Commons live image search"
    if category=="News": return news_search(query,page),"Google News live RSS"
    if category=="Videos": return video_search(query,page),"Dailymotion live video search"
    if category=="Maps": return map_search(query),"OpenStreetMap live places"
    return live_search(query,category,page,safe),"DuckDuckGo live web search"


def local_search(query, limit=8):
    terms = [x for x in re.findall(r"[\w'-]+", query) if len(x) > 1][:12]
    if not terms:
        return []
    match = " OR ".join('"' + t.replace('"', '""') + '"*' for t in terms)
    try:
        with db() as con:
            rows = con.execute("SELECT d.url,d.title,d.description,d.domain,bm25(document_search) score FROM document_search JOIN documents d ON d.id=document_search.rowid WHERE document_search MATCH ? ORDER BY score LIMIT ?", (match, limit)).fetchall()
        return [{"title": x["title"] or x["url"], "url": x["url"], "domain": x["domain"], "snippet": x["description"], "source": "NOVA index"} for x in rows]
    except sqlite3.OperationalError:
        return []


def log_search(query, category, results, elapsed, status="ok"):
    with db() as con:
        cur = con.execute("INSERT INTO searches(query,category,created_at,result_count,latency_ms,status) VALUES(?,?,?,?,?,?)", (query, category, utcnow(), len(results), elapsed, status))
        return cur.lastrowid


def is_public_host(host):
    try:
        return bool(public_host_addresses(host))
    except OSError:
        return False


def public_host_addresses(host):
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addresses = []
        for family, _, _, _, sockaddr in results:
            address = ipaddress.ip_address(sockaddr[0])
            if not address.is_global:
                raise OSError("Host resolves to a non-public address.")
            item = (family, str(address))
            if item not in addresses:
                addresses.append(item)
        if not addresses:
            raise OSError("Host has no addresses.")
        return addresses
    except (socket.gaierror, ValueError) as ex:
        raise OSError("Host could not be resolved to public addresses.") from ex


def create_public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    host, port = address
    resolved = public_host_addresses(host)
    last_error = None
    for family, ip in resolved:
        try:
            return socket.create_connection((ip, port), timeout, source_address)
        except OSError as ex:
            last_error = ex
    if last_error:
        raise last_error
    raise OSError("Host has no public addresses.")


class PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_public_connection


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_public_connection


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(PublicHTTPConnection, request)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=self._context, check_hostname=self._check_hostname)


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        target=urllib.parse.urljoin(request.full_url,newurl)
        parsed=urllib.parse.urlparse(target)
        if parsed.scheme not in ("http","https") or not parsed.hostname or not is_public_host(parsed.hostname):
            raise urllib.error.HTTPError(target,code,"Redirect target is not a public HTTP(S) host",headers,fp)
        return super().redirect_request(request,fp,code,message,headers,target)


def open_public(request, timeout):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        PublicHTTPHandler(),
        PublicHTTPSHandler(),
        PublicRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


class PageParser(HTMLParser):
    BLOCK = {"script", "style", "noscript", "svg", "nav", "footer", "header"}
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.description = ""
        self.text = []
        self.links = []
        self.images = []
        self.in_title = False
        self.block = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title": self.in_title = True
        if tag in self.BLOCK: self.block += 1
        if tag == "meta" and a.get("name", "").lower() in ("description", "og:description"):
            self.description = a.get("content", "")[:500]
        if tag == "a" and a.get("href"):
            u = urllib.parse.urljoin(self.base, a["href"])
            if urllib.parse.urlparse(u).scheme in ("http", "https"): self.links.append(u)
        if tag == "img" and a.get("src"):
            self.images.append(urllib.parse.urljoin(self.base, a["src"]))
    def handle_endtag(self, tag):
        if tag == "title": self.in_title = False
        if tag in self.BLOCK: self.block = max(0, self.block - 1)
    def handle_data(self, data):
        val = " ".join(data.split())
        if not val: return
        if self.in_title: self.title += val + " "
        if not self.block: self.text.append(val)


def crawl_one(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not is_public_host(parsed.hostname):
        raise ValueError("Only public HTTP and HTTPS websites can be crawled.")
    origin=f"{parsed.scheme}://{parsed.netloc}"
    if origin not in robots_cache:
        parser=urllib.robotparser.RobotFileParser();parser.set_url(origin+"/robots.txt")
        try:
            robots_req=urllib.request.Request(origin+"/robots.txt",headers={"User-Agent":"NOVA-Indexer/1.0"})
            with open_public(robots_req,8) as robots_response:
                parser.parse(robots_response.read(512_000).decode("utf-8","replace").splitlines())
        except urllib.error.HTTPError as ex:
            parser.parse(["User-agent: *", "Allow: /"] if ex.code==404 else ["User-agent: *", "Disallow: /"])
        except Exception:
            parser.parse(["User-agent: *", "Disallow: /"])
        robots_cache[origin]=parser
    if not robots_cache[origin].can_fetch("NOVA-Indexer",url):
        raise ValueError("Disallowed by the website's robots.txt rules.")
    req = urllib.request.Request(url, headers={"User-Agent": "NOVA-Indexer/1.0 (+local search prototype)", "Accept": "text/html,application/xhtml+xml"})
    with open_public(req, timeout=12) as response:
        if response.status >= 400: raise RuntimeError("Website returned HTTP " + str(response.status))
        if "text/html" not in response.headers.get("Content-Type", "").lower(): return [], None
        data = response.read(1_500_000)
        charset = response.headers.get_content_charset() or "utf-8"
        final_url = response.geturl()
    parser = PageParser(final_url)
    parser.feed(data.decode(charset, "replace"))
    title = " ".join(parser.title.split())[:300] or final_url
    text = " ".join(parser.text)
    desc = parser.description or text[:340]
    if len(text) > 1_500_000: text = text[:1_500_000]
    domain = urllib.parse.urlparse(final_url).hostname or ""
    now = utcnow()
    with db() as con:
        old = con.execute("SELECT id FROM documents WHERE url=?", (final_url,)).fetchone()
        if old:
            con.execute("DELETE FROM document_search WHERE rowid=?", (old["id"],))
            doc_id = old["id"]
            con.execute("UPDATE documents SET title=?,description=?,body=?,domain=?,crawled_at=?,status='indexed' WHERE id=?", (title, desc, text, domain, now, doc_id))
        else:
            cur = con.execute("INSERT INTO documents(url,title,description,body,domain,crawled_at) VALUES(?,?,?,?,?,?)", (final_url,title,desc,text,domain,now))
            doc_id = cur.lastrowid
        con.execute("INSERT INTO document_search(rowid,title,description,body,url,domain) VALUES(?,?,?,?,?,?)", (doc_id,title,desc,text,final_url,domain))
    next_links=[]
    for link in parser.links:
        p=urllib.parse.urlparse(link)
        if p.scheme in ("http","https") and p.hostname and not p.path.lower().endswith((".pdf",".jpg",".jpeg",".png",".gif",".zip",".mp4",".webp")):
            next_links.append(urllib.parse.urlunparse((p.scheme,p.netloc,p.path,"",p.query,"")))
    return list(dict.fromkeys(next_links)), {"url":final_url,"title":title,"domain":domain,"description":desc[:350],"images":parser.images[:40]}


def run_crawl(seeds, max_pages):
    seen=set(); queue=list(seeds); crawled=errors=0
    with db() as con:
        cur=con.execute("INSERT INTO crawler_jobs(seed,status,queued,created_at) VALUES(?,?,?,?)", (seeds[0],"running",len(queue),utcnow()))
        job_id=cur.lastrowid
    with crawler_lock:
        crawler.update(running=True, queued=len(queue), crawled=0, errors=0, current="", started_at=utcnow(), last_event="Crawl started")
    try:
        while queue and crawled < max_pages:
            url=queue.pop(0)
            if url in seen: continue
            seen.add(url)
            with crawler_lock: crawler.update(queued=len(queue), current=url)
            try:
                links,doc=crawl_one(url)
                if doc:
                    crawled+=1
                    with crawler_lock: crawler.update(crawled=crawled,last_event="Indexed "+doc["domain"])
                    host=urllib.parse.urlparse(doc["url"]).hostname
                    for link in links:
                        p=urllib.parse.urlparse(link)
                        if p.hostname==host and link not in seen and len(queue)<max_pages*4: queue.append(link)
                time.sleep(.4)
            except Exception as ex:
                errors+=1
                with crawler_lock: crawler.update(errors=errors,last_event="Skipped "+urllib.parse.urlparse(url).hostname+": "+str(ex)[:100])
            with db() as con:
                con.execute("UPDATE crawler_jobs SET queued=?,crawled=?,errors=? WHERE id=?",(len(queue),crawled,errors,job_id))
        status="completed" if not queue else "limit reached"
    finally:
        with db() as con:
            con.execute("UPDATE crawler_jobs SET status=?,queued=?,crawled=?,errors=?,finished_at=? WHERE id=?",(status if 'status' in locals() else 'stopped',len(queue),crawled,errors,utcnow(),job_id))
        with crawler_lock: crawler.update(running=False,queued=len(queue),crawled=crawled,errors=errors,current="",last_event="Crawl "+(status if 'status' in locals() else 'stopped'))


def analytics(now=None):
    now = now or datetime.now(timezone.utc)
    now_value = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    with db() as con:
        total=con.execute("SELECT count(*) FROM searches").fetchone()[0]
        zero=con.execute("SELECT count(*) FROM searches WHERE result_count=0 AND status='ok'").fetchone()[0]
        avg=con.execute("SELECT COALESCE(round(avg(latency_ms)),0) FROM searches WHERE status='ok'").fetchone()[0]
        top=con.execute("SELECT query,count(*) count FROM searches WHERE datetime(created_at)>=datetime(?, '-7 days') GROUP BY lower(query) ORDER BY count DESC LIMIT 8", (now_value,)).fetchall()
        recent=con.execute("SELECT id,query,category,created_at,result_count,latency_ms,status FROM searches ORDER BY id DESC LIMIT 12").fetchall()
        days=con.execute("SELECT strftime('%Y-%m-%d',created_at) day,count(*) count FROM searches WHERE datetime(created_at)>=datetime(?, '-6 days') GROUP BY day ORDER BY day", (now_value,)).fetchall()
        docs=con.execute("SELECT count(*) FROM documents").fetchone()[0]
        clicks=con.execute("SELECT count(*) FROM events WHERE kind='click'").fetchone()[0]
    return {"totalSearches":total,"zeroResultRate":round(zero/total*100,1) if total else 0,"avgLatencyMs":avg,"topQueries":[dict(x) for x in top],"recentSearches":[dict(x) for x in recent],"activity": [dict(x) for x in days],"indexedPages":docs,"clicks":clicks}


class Handler(BaseHTTPRequestHandler):
    server_version="NOVA/1.0"
    def end_headers(self):
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; connect-src 'self'; font-src 'self' data:; "
            "frame-src 'self' https://www.openstreetmap.org; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        super().end_headers()
    def log_message(self, fmt, *args):
        print("[%s] %s"%(self.log_date_time_string(),fmt%args))
    def send_json(self, obj, status=200):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
    def read_json(self):
        n=min(int(self.headers.get("Content-Length","0")),100_000)
        return json.loads(self.rfile.read(n) or b"{}")
    def dispatch_mail(self):
        parsed=urllib.parse.urlsplit(self.path)
        if parsed.path!="/mail" and not parsed.path.startswith("/mail/"):
            return False
        if parsed.path == "/mail/healthz":
            self.send_json({"ok": True, "service": "NOVA Orbit Mail"})
            return True
        if mail_app is None:
            self.send_error(503,"NOVA Orbit Mail is unavailable")
            return True
        path_info=urllib.parse.unquote(parsed.path[len("/mail"):]) or "/"
        environ={}
        setup_testing_defaults(environ)
        environ.update({
            "REQUEST_METHOD":self.command,
            "SCRIPT_NAME":"/mail",
            "PATH_INFO":path_info,
            "QUERY_STRING":parsed.query,
            "SERVER_NAME":self.server.server_name,
            "SERVER_PORT":str(self.server.server_port),
            "SERVER_PROTOCOL":self.request_version,
            "REMOTE_ADDR":self.client_address[0],
            "CONTENT_LENGTH":self.headers.get("Content-Length",""),
            "CONTENT_TYPE":self.headers.get("Content-Type",""),
            "wsgi.input":self.rfile,
            "wsgi.errors":sys.stderr,
            "wsgi.multithread":True,
            "wsgi.multiprocess":False,
            "wsgi.run_once":False,
        })
        for name,value in self.headers.items():
            normalized=name.upper().replace("-","_")
            if normalized not in {"CONTENT_LENGTH","CONTENT_TYPE"}:
                environ["HTTP_"+normalized]=value
        response={"status":"500 Internal Server Error","headers":[]}
        def start_response(status,headers,exc_info=None):
            if exc_info is not None and response.get("started"):
                raise exc_info[1].with_traceback(exc_info[2])
            response.update(status=status,headers=headers,started=True)
            return self.wfile.write
        result=mail_app(environ,start_response)
        try:
            self.send_response(int(response["status"].split(" ",1)[0]))
            for name,value in response["headers"]:
                self.send_header(name,value)
            self.end_headers()
            if self.command!="HEAD":
                for chunk in result:
                    self.wfile.write(chunk)
        finally:
            close=getattr(result,"close",None)
            if close:
                close()
        return True
    def do_GET(self):
        if self.dispatch_mail(): return
        u=urllib.parse.urlparse(self.path); q=urllib.parse.parse_qs(u.query)
        if u.path=="/":
            data=(HERE/"index.html").read_bytes(); self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data);return
        assets = {"/styles.css": "text/css; charset=utf-8", "/app.js": "text/javascript; charset=utf-8", "/category-selection.js": "text/javascript; charset=utf-8", "/favicon.svg": "image/svg+xml"}
        if u.path in assets:
            data=(HERE/u.path.lstrip("/")).read_bytes();self.send_response(200);self.send_header("Content-Type",assets[u.path]);self.send_header("Content-Length",str(len(data)));self.send_header("Cache-Control","no-cache");self.end_headers();self.wfile.write(data);return
        if u.path=="/api/health": return self.send_json({"ok":True,"provider":"DuckDuckGo HTML search","indexedPages":analytics()["indexedPages"]})
        if u.path=="/api/suggest":
            term=q.get("q",[""])[0].strip()[:160]
            if not term: return self.send_json({"suggestions":[]})
            try:
                req=urllib.request.Request("https://duckduckgo.com/ac/?"+urllib.parse.urlencode({"q":term,"type":"list"}),headers={"User-Agent":USER_AGENT})
                with urllib.request.urlopen(req,timeout=8) as r: raw=json.loads(r.read(100_000))
                if isinstance(raw,list) and len(raw)>1 and isinstance(raw[1],list):
                    phrases=[str(x) for x in raw[1] if isinstance(x,str)]
                elif isinstance(raw,list):
                    phrases=[str(x.get("phrase","")) for x in raw if isinstance(x,dict) and x.get("phrase")]
                else:
                    phrases=[]
                return self.send_json({"suggestions":phrases[:7]})
            except Exception: return self.send_json({"suggestions":[]})
        if u.path=="/api/analytics": return self.send_json(analytics())
        if u.path=="/api/crawler/status":
            with crawler_lock: state=dict(crawler)
            with db() as con: jobs=[dict(x) for x in con.execute("SELECT * FROM crawler_jobs ORDER BY id DESC LIMIT 8").fetchall()]
            state["indexedPages"]=analytics()["indexedPages"];state["jobs"]=jobs
            return self.send_json(state)
        if u.path=="/api/search":
            query=q.get("q",[""])[0].strip()[:240]; category=q.get("category",["Web"])[0]
            try: page=max(1,min(20,int(q.get("page",["1"])[0])))
            except (TypeError,ValueError): return self.send_json({"error":"Page must be a number from 1 to 20."},400)
            safe=q.get("safe",["1"])[0]!="0"
            if not query:return self.send_json({"error":"Enter a search query."},400)
            start=time.perf_counter();local=local_search(query) if category=="Web" else []
            try:
                remote,provider=category_search(query,category,page,safe); elapsed=round((time.perf_counter()-start)*1000)
                # Local pages are shown first only if they offer something beyond the live result set.
                live_urls={x["url"] for x in remote};extra=[x for x in local if x["url"] not in live_urls]
                results=extra+remote
                sid=log_search(query,category,results,elapsed)
                return self.send_json({"searchId":sid,"query":query,"category":category,"page":page,"results":results,"count":len(results),"latencyMs":elapsed,"provider":provider,"safeSearch":safe})
            except Exception as ex:
                elapsed=round((time.perf_counter()-start)*1000); sid=log_search(query,category,local,elapsed,"provider_error")
                if local:return self.send_json({"searchId":sid,"query":query,"category":category,"page":page,"results":local,"count":len(local),"latencyMs":elapsed,"provider":"NOVA local index","warning":"Live search is unavailable right now; showing locally indexed pages."})
                return self.send_json({"error":"Live search is temporarily unavailable: "+str(ex),"searchId":sid,"latencyMs":elapsed},502)
        if u.path=="/api/index":
            term=q.get("q",[""])[0]; return self.send_json({"results":local_search(term,30)})
        self.send_error(404)
    def do_POST(self):
        if self.dispatch_mail(): return
        u=urllib.parse.urlparse(self.path)
        try: payload=self.read_json()
        except Exception:return self.send_json({"error":"Invalid JSON."},400)
        if u.path=="/api/privacy/clear-analytics":
            with db() as con:
                con.execute("DELETE FROM searches")
                con.execute("DELETE FROM events")
            return self.send_json({"ok":True})
        if u.path=="/api/click":
            try:
                with db() as con: con.execute("UPDATE searches SET clicked_url=? WHERE id=?",(str(payload.get("url",""))[:2000],int(payload.get("searchId",0))))
                with db() as con: con.execute("INSERT INTO events(kind,detail,created_at) VALUES('click',?,?)",(str(payload.get("url",""))[:2000],utcnow()))
                return self.send_json({"ok":True})
            except Exception:return self.send_json({"ok":False},400)
        if u.path=="/api/crawler/start":
            seeds=payload.get("urls",[]); seeds=seeds if isinstance(seeds,list) else [seeds]
            seeds=[str(x).strip()[:2048] for x in seeds if str(x).strip()][:15]
            try: max_pages=max(1,min(300,int(payload.get("maxPages",50))))
            except (TypeError,ValueError): return self.send_json({"error":"Maximum pages must be a number from 1 to 300."},400)
            if not seeds:return self.send_json({"error":"Add at least one website URL."},400)
            with crawler_lock:
                if crawler["running"]:return self.send_json({"error":"A crawl is already running."},409)
                crawler["running"]=True
            thread=threading.Thread(target=run_crawl,args=(seeds,max_pages),daemon=True);thread.start()
            return self.send_json({"ok":True,"message":"Crawler started","maxPages":max_pages})
        self.send_error(404)
    def do_HEAD(self):
        if not self.dispatch_mail(): self.send_error(404)
    def do_PUT(self):
        if not self.dispatch_mail(): self.send_error(404)
    def do_PATCH(self):
        if not self.dispatch_mail(): self.send_error(404)
    def do_DELETE(self):
        if not self.dispatch_mail(): self.send_error(404)
    def do_OPTIONS(self):
        if not self.dispatch_mail(): self.send_error(404)


if __name__=="__main__":
    from combined_server import main
    raise SystemExit(main())
