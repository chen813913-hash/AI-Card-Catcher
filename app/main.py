#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-Card-Catcher - one-process control center
============================================
Ports:
  3000  New API           (child process, bin/new-api.exe)
  3001  this program      (recording proxy + admin web UI)
  cpolar child process    (public tunnel -> 127.0.0.1:3001)

Routes on 3001:
  /v1/*        forwarded to New API, request/response fully recorded
  /cc/api/*    admin JSON api (token protected)
  /            admin web UI (web/index.html)

Pure standard library. Python 3.10+.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

# ---------------------------------------------------------------- paths
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(BASE, "bin")
DATA = os.path.join(BASE, "data")
WEB = os.path.join(BASE, "web")
CONFIG_PATH = os.path.join(DATA, "config.json")
DB_PATH = os.path.join(DATA, "records.db")
CPOLAR_YML = os.path.join(DATA, "cpolar.yml")
LOG_NEWAPI = os.path.join(DATA, "newapi.log")
LOG_CPOLAR = os.path.join(DATA, "cpolar.log")
BACKUP_DIR = os.path.join(DATA, "backups")
os.makedirs(DATA, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

OLD_DB = os.path.join(os.path.dirname(BASE), "request_logs.db")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
    "content-length", "host", "date",
}
MAX_STORE = 50_000_000
READ_CHUNK = 8192
UPSTREAM_TIMEOUT = 600

DEFAULT_CONFIG = {
    "newapi_port": 3000,
    "recorder_port": 3001,
    "session_secret": "",
    "view_token": "",
    "cpolar": {"authtoken": "", "region": "cn", "subdomain": ""},
    "access_token_name": "VSOUL专用",
    "auto_start_newapi": True,
    "auto_start_cpolar": True,
    "open_browser": True,
    "link_chat_url": "http://127.0.0.1:5173",
    "classify_rules": {
        "strong": [
            ["character", ["immutable role-play character settings",
                           "Here is your immutable"]],
            ["worldbook", ["世界观知识", "# 世界观", "World Info", "world info"]],
            ["player", ["扮演的身份信息", "User 扮演", "persona"]],
            ["rules", ["# 补充设定", "协作规则", "叙事视角"]],
            ["nsfw_guide", ["情色描写", "成人虚构", "描写指导"]],
        ],
        "weak": [["character", ["角色设定", "角色卡"]]],
        "big_user_worldbook": 20000,
        "big_any_worldbook": 3000,
    },
}

# RLock (reentrant) is REQUIRED: load_config() holds this lock and calls
# save_config() which acquires it again - a plain Lock deadlocks here, and
# that deadlock hits every first-time user (no config.json yet).
CONFIG_LOCK = threading.RLock()


def load_config():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_PATH):
            import secrets
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["session_secret"] = secrets.token_hex(32)
            cfg["view_token"] = secrets.token_urlsafe(18)
            save_config(cfg)
            return cfg
        cfg = json.load(open(CONFIG_PATH, encoding="utf8"))
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, json.loads(json.dumps(v)))
        return cfg


def save_config(cfg):
    with CONFIG_LOCK:
        with open(CONFIG_PATH, "w", encoding="utf8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        global CFG
        CFG = cfg                      # keep in-memory copy in sync


CFG = load_config()

# ---------------------------------------------------------------- database
_db_lock = threading.Lock()


def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


APP_DB = os.path.join(BASE, "one-api.db")     # New API's own database


def appdb_conn():
    conn = sqlite3.connect(APP_DB, timeout=20, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    first = not os.path.exists(DB_PATH)
    with _db_lock, db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, method TEXT, path TEXT, model TEXT,
                token TEXT, client_ip TEXT, status INTEGER, elapsed_ms INTEGER,
                stream INTEGER DEFAULT 0, req_body TEXT, resp_body TEXT,
                resp_text TEXT, system_prompt TEXT, error TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rl_id ON request_logs(id DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS deleted_records (
            id INTEGER PRIMARY KEY, deleted_at TEXT NOT NULL,
            deleted_reason TEXT DEFAULT 'user'
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted_at ON deleted_records(deleted_at DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS card_resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_type TEXT NOT NULL,
                name TEXT NOT NULL,
                current_version_id INTEGER,
                first_source_id INTEGER,
                latest_source_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT,
                cloned_from_id INTEGER
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS card_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id INTEGER NOT NULL,
                resource_type TEXT NOT NULL,
                name TEXT NOT NULL,
                content_json TEXT NOT NULL,
                content_fingerprint TEXT NOT NULL,
                version_kind TEXT NOT NULL DEFAULT 'original',
                source_record_id INTEGER,
                created_at TEXT NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(resource_id) REFERENCES card_resources(id)
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_resources_type ON card_resources(resource_type, deleted_at, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_versions_resource ON card_versions(resource_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_versions_source ON card_versions(source_record_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS card_resource_sources (
                resource_id INTEGER NOT NULL,
                source_record_id INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY(resource_id, source_record_id),
                FOREIGN KEY(resource_id) REFERENCES card_resources(id)
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_sources_record ON card_resource_sources(source_record_id)")
    if first and os.path.exists(OLD_DB):
        try:
            shutil.copy(OLD_DB, DB_PATH + ".old.bak")
            print("[migrate] imported %d old records" %
                  _count_old(), file=sys.stderr)
        except Exception:
            pass


def _count_old():
    return 0  # informational only


def migrate_old_records():
    """Import records from the pre-project recorder db (F:/NewAPI/request_logs.db)."""
    old = OLD_DB
    if not os.path.exists(old):
        return
    try:
        with _db_lock, db_conn() as conn:
            n = conn.execute("select count(*) from request_logs").fetchone()[0]
            if n > 0:
                return
            on = conn.execute(
                "attach database ? as old", (old,))
            try:
                cnt = conn.execute(
                    "select count(*) from old.request_logs").fetchone()[0]
                if cnt:
                    conn.execute(
                        "insert into main.request_logs "
                        "select * from old.request_logs")
                    print("[migrate] imported %d old records" % cnt)
            finally:
                conn.execute("detach database old")
    except Exception as e:
        print("[migrate] failed: %r" % (e,))


def backup_records_db():
    """Create a consistent SQLite backup, including WAL state, on demand."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, "records_%s.db" % stamp)
    try:
        with _db_lock:
            src = db_conn()
            try:
                out = sqlite3.connect(dest)
                try:
                    src.backup(out)
                finally:
                    out.close()
            finally:
                src.close()
        # Keep the newest 10 backups to avoid silently filling the disk.
        files = sorted((os.path.join(BACKUP_DIR, x)
                        for x in os.listdir(BACKUP_DIR)
                        if x.startswith("records_") and x.endswith(".db")),
                       key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[10:]:
            try:
                os.remove(old)
            except OSError:
                pass
        return True, dest
    except Exception as e:
        return False, repr(e)


def soft_delete_records(ids):
    """Move records into an in-database trash table; reversible."""
    clean = []
    for value in ids:
        try:
            rid = int(value)
            if rid > 0 and rid not in clean:
                clean.append(rid)
        except (TypeError, ValueError):
            pass
    if not clean or len(clean) > 50:
        return False, "请选择 1-50 条记录"
    marks = ",".join("?" for _ in clean)
    with _db_lock, db_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS deleted_records (
            id INTEGER PRIMARY KEY, deleted_at TEXT NOT NULL,
            deleted_reason TEXT DEFAULT 'user'
        )""")
        conn.executemany(
            "INSERT OR REPLACE INTO deleted_records(id,deleted_at) VALUES(?,?)",
            [(rid, datetime.now().strftime("%Y-%m-%d %H:%M:%S")) for rid in clean])
        conn.commit()
    return True, clean


def restore_records(ids):
    clean = []
    for value in ids:
        try:
            rid = int(value)
            if rid > 0 and rid not in clean:
                clean.append(rid)
        except (TypeError, ValueError):
            pass
    if not clean or len(clean) > 50:
        return False, "请选择 1-50 条记录"
    marks = ",".join("?" for _ in clean)
    with _db_lock, db_conn() as conn:
        conn.execute("DELETE FROM deleted_records WHERE id IN (%s)" % marks,
                     clean)
        conn.commit()
    return True, clean


def purge_records(ids):
    clean = []
    for value in ids:
        try:
            rid = int(value)
            if rid > 0 and rid not in clean:
                clean.append(rid)
        except (TypeError, ValueError):
            pass
    if not clean or len(clean) > 50:
        return False, "请选择 1-50 条记录"
    marks = ",".join("?" for _ in clean)
    with _db_lock, db_conn() as conn:
        conn.execute("DELETE FROM request_logs WHERE id IN (%s)" % marks,
                     clean)
        conn.execute("DELETE FROM deleted_records WHERE id IN (%s)" % marks,
                     clean)
        conn.commit()
    return True, clean


def save_log(rec):
    try:
        with _db_lock, db_conn() as conn:
            conn.execute(
                """INSERT INTO request_logs
                   (ts,method,path,model,token,client_ip,status,elapsed_ms,
                    stream,req_body,resp_body,resp_text,system_prompt,error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.get("ts"), rec.get("method"), rec.get("path"),
                 rec.get("model"), rec.get("token"), rec.get("client_ip"),
                 rec.get("status"), rec.get("elapsed_ms"), rec.get("stream", 0),
                 rec.get("req_body"), rec.get("resp_body"),
                 rec.get("resp_text"), rec.get("system_prompt"), rec.get("error")))
    except Exception as exc:
        sys.stderr.write("[db] save_log failed: %r\n" % (exc,))


# ---------------------------------------------------------------- processes
PROCS = {}          # name -> subprocess.Popen
PROC_LOCK = threading.Lock()
MAIN_SERVER = None  # set in main(); used by the shutdown endpoint


def shutdown_everything():
    """Stop cpolar + New API, then stop the web server (process exits)."""
    print("[shutdown] stopping services...")
    stop_cpolar()
    stop_newapi()
    if MAIN_SERVER is not None:
        threading.Thread(target=MAIN_SERVER.shutdown, daemon=True).start()


def _exe(name):
    p = os.path.join(BIN, name)
    return p if os.path.exists(p) else None


def newapi_running():
    p = PROCS.get("newapi")
    return p is not None and p.poll() is None


def cpolar_running():
    p = PROCS.get("cpolar")
    return p is not None and p.poll() is None


def start_newapi():
    if newapi_running():
        return True, "already running"
    exe = _exe("new-api.exe")
    if not exe:
        return False, "bin/new-api.exe not found - run get-binaries.bat"
    env = dict(os.environ)
    env["TZ"] = "Asia/Shanghai"
    env["PORT"] = str(CFG["newapi_port"])
    env["SESSION_SECRET"] = CFG["session_secret"] or "aicardcatcher-secret"
    logf = open(LOG_NEWAPI, "ab")
    PROCS["newapi"] = subprocess.Popen(
        [exe], cwd=BASE, env=env,
        stdout=logf, stderr=subprocess.STDOUT)
    return True, "started"


def stop_newapi():
    p = PROCS.pop("newapi", None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
    subprocess.run(["taskkill", "/IM", "new-api.exe", "/F"],
                   capture_output=True)
    return True, "stopped"


def start_cpolar():
    if cpolar_running():
        return True, "already running"
    exe = _exe("cpolar.exe")
    if not exe:
        return False, "bin/cpolar.exe not found - run get-binaries.bat"
    tok = (CFG.get("cpolar", {}).get("authtoken") or "").strip()
    if not tok:
        return False, "cpolar authtoken not configured (config page)"
    with open(CPOLAR_YML, "w", encoding="utf8") as f:
        f.write("authtoken: %s\n" % tok)
    region = CFG.get("cpolar", {}).get("region") or "cn"
    logf = open(LOG_CPOLAR, "wb")
    PROCS["cpolar"] = subprocess.Popen(
        [exe, "http", "-region", region, "-log", "stdout",
         "-log-level", "info", "-config", CPOLAR_YML,
         str(CFG["recorder_port"])],
        cwd=BASE, stdout=logf, stderr=subprocess.STDOUT)
    return True, "started"


def stop_cpolar():
    p = PROCS.pop("cpolar", None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
    subprocess.run(["taskkill", "/IM", "cpolar.exe", "/F"],
                   capture_output=True)
    return True, "stopped"


def read_public_url():
    """Extract the newest tunnel URL from cpolar log file."""
    try:
        t = open(LOG_CPOLAR, "rb").read().decode("utf8", "ignore")
    except Exception:
        return ""
    t = re.sub(r"\x1b\[[0-9;]*m", "", t)
    urls = re.findall(
        r'Tunnel established at (https://[a-zA-Z0-9.-]+\.cpolar\.cn)', t)
    return urls[-1] if urls else ""


def port_open(port, host="127.0.0.1"):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------- card utils
def _msg_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict))
    return ""


# strong markers first; weak markers ("角色设定" appears inside other text)
# only apply when nothing strong matched. Very long user messages fall back
# to worldbook because roleplay sites ship lore as big user blobs.
# Rules are overridable in config.json (classify_rules) so users can adapt
# to new sites without touching code.
DEFAULT_RULES = json.loads(json.dumps(DEFAULT_CONFIG["classify_rules"]))


def _rules():
    r = CFG.get("classify_rules") or {}
    strong = r.get("strong") or DEFAULT_RULES["strong"]
    weak = r.get("weak") or DEFAULT_RULES["weak"]
    big = r.get("big_user_worldbook", DEFAULT_RULES["big_user_worldbook"])
    big_any = r.get("big_any_worldbook", DEFAULT_RULES["big_any_worldbook"])
    return strong, weak, int(big), int(big_any)


def detect_format(req):
    """Identify OpenAI / Anthropic / Gemini native request shapes."""
    if not isinstance(req, dict):
        return "unknown"
    if isinstance(req.get("messages"), list):
        msgs = req["messages"]
        roles = {m.get("role") for m in msgs if isinstance(m, dict)}
        if req.get("system") is not None and roles <= {"user", "assistant"}:
            return "anthropic"
        return "openai"
    if isinstance(req.get("contents"), list):
        return "gemini"
    if "prompt" in req:
        return "prompt"
    return "unknown"


def unified_messages(req):
    """Normalize any supported format into [(role, text), ...]."""
    fmt = detect_format(req)
    out = []
    if fmt == "anthropic":
        sysf = req.get("system")
        if isinstance(sysf, str) and sysf:
            out.append(("system", sysf))
        elif isinstance(sysf, list):
            out.append(("system", _msg_text(sysf)))
        for m in req.get("messages", []):
            if isinstance(m, dict):
                out.append((m.get("role", "user"), _msg_text(m.get("content"))))
    elif fmt == "gemini":
        si = req.get("systemInstruction") or req.get("system_instruction")
        if isinstance(si, dict):
            t = _msg_text(si.get("parts"))
            if t:
                out.append(("system", t))
        for c in req.get("contents", []):
            if not isinstance(c, dict):
                continue
            role = c.get("role", "user")
            if role == "model":
                role = "assistant"
            out.append((role, _msg_text(c.get("parts"))))
    else:                                    # openai (also covers prompt fmt)
        for m in req.get("messages", []):
            if isinstance(m, dict):
                out.append((m.get("role", "user"), _msg_text(m.get("content"))))
        if not out and isinstance(req.get("prompt"), str):
            out.append(("user", req["prompt"]))
    return fmt, out


def _embedded_markers(text):
    """Find high-confidence section starts inside one message.

    Do not split on every Markdown heading: jailbreak prompts commonly contain
    many # headings of their own.  Only markers that identify one of the card
    payload blocks are treated as boundaries.
    """
    patterns = [
        # **名称** is the stable character-card boundary. The optional
        # csref comment is folded into this same block below.
        (r"(?m)^\s*\*\*名称\*\*\s*[:：]", "character"),
        (r"(?mi)^\s*#\s*User\s*扮演的身份信息\s*$", "player"),
        (r"(?mi)^\s*#\s*(?:玩家|用户)\s*(?:身份|人设|persona)\s*(?:信息)?\s*$", "player"),
        (r"(?mi)^\s*#\s*(?:补充设定|补充设定/协作规则|协作规则)(?:/[^\n]*)?\s*$", "rules"),
        (r"(?mi)^\s*#\s*(?:世界观知识|世界书|世界观|world\s*info)\s*:?(?:\s*)$", "worldbook"),
        (r"(?mi)^\s*#\s*(?:描写指导|成人描写指导|nsfw\s*guide)\s*:?(?:\s*)$", "nsfw_guide"),
        (r"(?mi)^\s*(?:系统破甲提示词|系统提示词|jailbreak(?:\s*prompt)?|system\s*prompt)\s*[:：]?\s*$", "jailbreak"),
    ]
    found = []
    for pattern, kind in patterns:
        for match in re.finditer(pattern, text):
            found.append((match.start(), kind, match.group(0)))
    # A csref comment and the following **名称** describe one block.  Keep
    # the earlier comment as the block start and discard the duplicate name
    # start only when it is immediately after the comment.
    found.sort(key=lambda x: (x[0], -len(x[2])))
    unique = []
    for item in found:
        if unique and item[0] == unique[-1][0]:
            continue
        if unique and item[0] < unique[-1][0] + len(unique[-1][2]):
            continue
        unique.append(item)
    return unique


def _split_embedded_sections(text):
    """Return [(content, explicit_type)] for recognizable inner blocks.

    The original text is sliced, never reconstructed, so whitespace, Unicode
    punctuation, and provider-specific formatting remain intact.  A preamble
    before the first marker is retained as its own untyped section.
    """
    markers = _embedded_markers(text)
    if not markers:
        return [(text, None)]
    chunks = []
    first_start = markers[0][0]
    # Some providers put an HTML csref comment immediately before the
    # character title. It is metadata for that title, not a standalone
    # jailbreak preamble, so move the boundary back to the comment.
    prefix = text[:first_start]
    if markers[0][1] == "character":
        comment = re.search(r"<!--\s*csref:[^>]+-->\s*$", prefix, re.I)
        if comment and not prefix[:comment.start()].strip():
            first_start = comment.start()
    if first_start > 0:
        chunks.append((text[:first_start], None))
    for pos, (start, kind, _marker) in enumerate(markers):
        if pos == 0:
            start = first_start
        end = markers[pos + 1][0] if pos + 1 < len(markers) else len(text)
        if end <= start:
            continue
        content = text[start:end]
        # VSOUL emits an HTML csref comment directly before the character
        # title. Include it in the character slice instead of making a tiny
        # second section that would inflate the character count.
        chunks.append((content, kind))
    return chunks


def _classify_message(role, text, index, last_idx, explicit=None):
    """Classify one complete message or one embedded section."""
    if explicit:
        return explicit
    if role in ("system", "developer"):
        return "jailbreak"
    if role == "assistant":
        return "history"
    strong, weak, big_user, big_any = _rules()
    low = text[:300].lower()
    kind = None
    for name, keys in strong:
        if any(k.lower() in low for k in keys):
            kind = name
            break
    if kind is None and len(text) > big_user:
        kind = "worldbook"
    if kind is None:
        for name, keys in weak:
            if any(k.lower() in low for k in keys):
                kind = name
                break
    if kind is None:
        if len(text) > big_any:
            kind = "worldbook"
        elif index == last_idx:
            kind = "user_input"
        else:
            kind = "history"
    return kind


def classify_sections(pairs):
    """Classify messages and recognizable blocks nested inside each message.

    ``index`` remains the parent message index for compatibility.  New fields
    ``message_index`` and ``section_index`` identify the exact nested block,
    allowing the UI/exporters to distinguish multiple categories in one role.
    """
    sections = []
    last_idx = len(pairs) - 1
    for i, (role, text) in enumerate(pairs):
        chunks = _split_embedded_sections(text)
        # A marker that starts at position 0 still produces one chunk; the
        # explicit type is enough to override the role-level system fallback.
        for sub_idx, (content, explicit) in enumerate(chunks):
            if not content:
                continue
            kind = _classify_message(role, content, i, last_idx, explicit)
            sections.append({
                "type": kind,
                "role": role,
                "index": i,
                "message_index": i,
                "section_index": sub_idx,
                "length": len(content),
                "content": content,
            })
    return sections


def classify_request(req):
    """Detect format, normalize, classify. Returns (format, sections)."""
    fmt, pairs = unified_messages(req)
    return fmt, classify_sections(pairs)


TYPE_CN = {
    "jailbreak": "系统破甲提示词", "character": "角色设定", "worldbook": "世界书/世界观知识",
    "player": "玩家身份设定", "rules": "补充设定与协作规则", "nsfw_guide": "描写指导(破甲)",
    "greeting": "开场白", "history": "聊天历史", "user_input": "用户输入",
}


def first_of(sections, kind):
    for s in sections:
        if s["type"] == kind:
            return s["content"]
    return ""


def all_of(sections, kind):
    return "\n\n---\n\n".join(s["content"] for s in sections
                              if s["type"] == kind and s["content"])


def build_jailbreak(sections, model="", record_id=None):
    """Export only system-level jailbreak/instruction sections as JSON."""
    items = [s for s in sections
             if s["type"] in ("jailbreak", "nsfw_guide") and s.get("content")]
    return {
        "format": "ai-card-catcher-jailbreak",
        "version": "1.0",
        "record_id": record_id,
        "model": model,
        "system_prompt": "\n\n---\n\n".join(
            s["content"] for s in items if s["type"] == "jailbreak"),
        "post_history_instructions": "\n\n---\n\n".join(
            s["content"] for s in items if s["type"] == "nsfw_guide"),
        "sections": [
            {"type": s["type"], "label": TYPE_CN.get(s["type"], s["type"]),
             "role": s["role"], "index": s["index"],
             "message_index": s.get("message_index", s["index"]),
             "section_index": s.get("section_index", 0),
             "length": s["length"], "content": s["content"]}
            for s in items
        ],
    }


def build_st_v2(sections, model=""):
    greeting = ""
    greetings = []
    for s in sections:
        if s["role"] == "assistant":
            if not greeting:
                greeting = s["content"]
            elif s["content"]:
                greetings.append(s["content"])
    book = []
    for kind, title in (("worldbook", "世界观知识"),
                        ("rules", "补充设定/协作规则")):
        content = first_of(sections, kind)
        if content:
            book.append({"keys": [], "secondary_keys": [],
                         "content": content, "comment": title,
                         "enabled": True, "insertion_order": len(book)})
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "",
            "description": first_of(sections, "character"),
            "personality": "",
            "scenario": "",
            "first_mes": greeting,
            "mes_example": "",
            "creator_notes": "Captured by AI-Card-Catcher from " + model,
            "system_prompt": first_of(sections, "jailbreak"),
            "post_history_instructions": first_of(sections, "nsfw_guide"),
            "alternate_greetings": greetings[1:] if len(greetings) > 1 else [],
            "tags": [],
            "creator": "AI-Card-Catcher",
            "character_version": "",
            "character_book": {"entries": book} if book else {"entries": []},
            "extensions": {
                "world_book_raw": first_of(sections, "worldbook"),
                "player_persona": first_of(sections, "player"),
                "extra_rules": first_of(sections, "rules"),
                "captured_model": model,
            },
        },
    }


def build_vsoul(sections, model=""):
    return {
        "角色名": "", "显示名": "", "性别": "", "身份分类": "",
        "角色描述": first_of(sections, "character"),
        "性格": "", "对话场景": "", "背景知识": "",
        "开场白": first_of(sections, "greeting") or first_of(sections, "history"),
        "角色简介": "",
        "世界书": [{"标题": "世界观知识(自动提取)",
                   "内容": first_of(sections, "worldbook")}],
        "系统破甲提示词": first_of(sections, "jailbreak"),
        "补充设定与协作规则": all_of(sections, "rules"),
        "描写指导": first_of(sections, "nsfw_guide"),
        "玩家身份信息": first_of(sections, "player"),
        "来源模型": model,
        "标签": [],
    }


def build_worldbook(sections, model=""):
    """SillyTavern-style world book JSON."""
    entries = {}
    uid = 0
    for kind, title in (("worldbook", "世界观知识"),
                        ("rules", "补充设定/协作规则"),
                        ("character", "角色设定")):
        content = first_of(sections, kind)
        if not content:
            continue
        entries[str(uid)] = {
            "uid": uid, "key": [], "keysecondary": [],
            "comment": title + "(AI-Card-Catcher)", "content": content,
            "constant": True, "selective": False, "order": 100,
            "position": 1, "disable": False, "excludeRecursion": False,
            "preventRecursion": False, "probability": 100,
            "useProbability": True, "depth": 1, "group": "",
        }
        uid += 1
    return {"entries": entries}


def _section_text(sections, kinds):
    return "\n\n---\n\n".join(s.get("content", "") for s in sections
                              if s.get("type") in kinds and s.get("content"))


def _normalize_link_lorebook(content, title, source_kind="worldbook"):
    """Convert captured text into the chat app's editable lorebook shape."""
    if not content:
        return None
    return {
        "title": (title or "导入世界书")[:120],
        "description": "AI-Card-Catcher 导入的世界书",
        "tags": ["AI-Card-Catcher", source_kind],
        "entries": [{
            "id": "",
            "name": title or "导入条目",
            "keys": [], "content": content,
            "constant": True, "position": "after",
            "probability": 100, "depth": 1, "order": 100,
        }],
    }


def build_link_import(sections, model="", record_id=None):
    """Build the versioned packet consumed by ai-character-chat.

    Keep raw text and editable fields together so future data types can be added
    without breaking old packets. The destination decides embedded/global scope.
    """
    character = first_of(sections, "character")
    player = first_of(sections, "player")
    rules = first_of(sections, "rules")
    worldbook = first_of(sections, "worldbook")
    jailbreak = first_of(sections, "jailbreak")
    nsfw = first_of(sections, "nsfw_guide")
    char_name = ""
    m = re.search(r"(?m)^\s*\*\*名称\*\*\s*[:：]\s*(.+)$", character or "")
    if m:
        char_name = m.group(1).strip()[:120]
    char_content = character
    if char_content and m:
        char_content = re.sub(r"(?m)^\s*\*\*名称\*\*\s*[:：].*$", "", char_content, count=1).strip()
    jb_content = _section_text(sections, ("jailbreak", "nsfw_guide"))
    return {
        "type": "ai-card-catcher-import", "version": 1,
        "source": {"recordId": record_id, "model": model or "", "format": "captured"},
        "characters": [{
            "name": char_name or "导入角色",
            "displayName": char_name or "导入角色",
            "description": char_content or "",
            "personality": "", "scenario": "", "setting": char_content or "",
            "first_mes": "", "alternate_greetings": [], "mes_example": "",
            "system_prompt": "", "post_history_instructions": "",
            "creator_notes": "由 AI-Card-Catcher 抓取导入",
            "lorebook": {"entries": []}, "props": [],
        }] if character else [],
        "personas": [{
            "name": "抓取的用户马甲", "persona_text": player,
            "notes": "由 AI-Card-Catcher 从请求中的玩家身份区块提取",
        }] if player else [],
        "jailbreaks": [{
            "title": "抓取破甲 - " + (model or "未命名模型"),
            "type": "simple", "description": "从 AI 请求提取的破甲提示词",
            "systemPrompt": jailbreak, "rulePrompt": "", "postPrompt": nsfw,
            "status": "draft", "enabled": False,
        }] if jb_content else [],
        "lorebooks": [{
            **(_normalize_link_lorebook(worldbook, "抓取世界观", "worldbook") or {}),
            "scopeHint": "global",
        }] if worldbook else [],
        "rules": [{"content": rules, "defaultTarget": "character_system_prompt"}] if rules else [],
        "options": {
            "characterLorebookMode": "embedded", "globalLorebookMode": "global",
            "enableJailbreak": False, "rulesTarget": "character_system_prompt",
        },
    }


# ---------------------------------------------------------------- resource library
# The library is a materialized view over immutable request_logs plus editable
# card versions. request_logs is never rewritten by card operations.
LIBRARY_TYPES = ("character", "jailbreak", "worldbook", "player")
LIBRARY_LABELS = {
    "character": "人物卡", "jailbreak": "破甲",
    "worldbook": "世界书", "player": "用户马甲",
}


def _library_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _library_fingerprint(value):
    """Fingerprint editable payload, excluding capture-only source metadata."""
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items() if k != "source"}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        return obj
    raw = json.dumps(clean(value), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf8")).hexdigest()


def _library_name(resource_type, content, model="", record_id=None):
    if resource_type == "character":
        name = content.get("name") or content.get("displayName")
    elif resource_type in ("jailbreak", "worldbook"):
        name = content.get("title") or content.get("name")
    else:
        name = content.get("name") or content.get("title")
    name = str(name or "").strip()
    if name:
        return name[:160]
    fallback = {
        "character": "未命名人物卡", "jailbreak": "未命名破甲",
        "worldbook": "未命名世界书", "player": "未命名用户马甲",
    }.get(resource_type, "未命名资源")
    return "%s #%s" % (fallback, record_id or "new")


def _library_extracted(req, model, record_id):
    """Create one editable resource payload per supported category."""
    fmt, sections = classify_request(req)
    by = {}
    for sec in sections:
        by.setdefault(sec.get("type"), []).append(sec.get("content", ""))

    def joined(kind):
        return "\n\n---\n\n".join(x for x in by.get(kind, []) if x)

    out = []
    character = joined("character")
    if character:
        name = ""
        match = re.search(r"(?m)^\s*\*\*名称\*\*\s*[:：]\s*(.+)$", character)
        if match:
            name = match.group(1).strip()[:160]
            character_body = re.sub(
                r"(?m)^\s*\*\*名称\*\*\s*[:：].*$", "", character,
                count=1).strip()
        else:
            character_body = character
        out.append(("character", {
            "name": name or "导入角色", "displayName": name or "导入角色",
            "description": character_body, "personality": "", "scenario": "",
            "setting": character_body, "first_mes": "", "alternate_greetings": [],
            "mes_example": "", "creator_notes": "由 AI-Card-Catcher 抓取导入",
            "lorebook": {"entries": []}, "props": [],
            "source": {"recordId": record_id, "model": model or "", "format": fmt},
        }))

    jailbreak = "\n\n---\n\n".join(
        x for kind in ("jailbreak", "nsfw_guide") for x in by.get(kind, []) if x)
    if jailbreak:
        out.append(("jailbreak", {
            "title": "抓取破甲 - " + (model or "未命名模型"), "type": "simple",
            "description": "从 AI 请求提取的破甲提示词", "systemPrompt": joined("jailbreak"),
            "rulePrompt": "", "postPrompt": joined("nsfw_guide"),
            "status": "draft", "enabled": False,
            "source": {"recordId": record_id, "model": model or "", "format": fmt},
        }))

    worldbook = joined("worldbook")
    if worldbook:
        book = _normalize_link_lorebook(worldbook, "抓取世界观", "worldbook")
        book["scopeHint"] = "global"
        book["source"] = {"recordId": record_id, "model": model or "", "format": fmt}
        out.append(("worldbook", book))

    player = joined("player")
    if player:
        out.append(("player", {
            "name": "抓取的用户马甲", "persona_text": player,
            "notes": "由 AI-Card-Catcher 从请求中的玩家身份区块提取",
            "source": {"recordId": record_id, "model": model or "", "format": fmt},
        }))
    return out


def _library_add_source(conn, resource_id, source_id, now):
    if source_id:
        conn.execute("""INSERT OR IGNORE INTO card_resource_sources
            (resource_id,source_record_id,first_seen_at) VALUES(?,?,?)""",
                     (resource_id, source_id, now))


def sync_card_library():
    """Materialize new request records without changing old requests or edits."""
    with _db_lock, db_conn() as conn:
        rows = conn.execute("""SELECT r.id,r.req_body,r.model
            FROM request_logs r LEFT JOIN deleted_records d ON d.id=r.id
            WHERE d.id IS NULL AND r.req_body IS NOT NULL
            ORDER BY r.id ASC""").fetchall()
        for record_id, raw, model in rows:
            try:
                req = json.loads(raw or "{}")
                extracted = _library_extracted(req, model or "", record_id)
            except Exception:
                continue
            for resource_type, content in extracted:
                name = _library_name(resource_type, content, model, record_id)
                fingerprint = _library_fingerprint(content)
                link = conn.execute("""SELECT resource_id FROM card_resource_sources
                    WHERE source_record_id=? AND resource_id IN
                    (SELECT id FROM card_resources WHERE resource_type=?)""",
                                    (record_id, resource_type)).fetchone()
                if link:
                    continue
                resource = conn.execute("""SELECT r.id,r.current_version_id FROM card_resources r
                    LEFT JOIN card_versions v ON v.resource_id=r.id
                    WHERE r.resource_type=? AND (r.name=? OR v.name=?) AND r.deleted_at IS NULL
                    ORDER BY r.id DESC LIMIT 1""", (resource_type, name, name)).fetchone()
                now = _library_now()
                if not resource:
                    conn.execute("""INSERT INTO card_resources
                        (resource_type,name,created_at,updated_at,latest_source_id)
                        VALUES(?,?,?,?,?)""", (resource_type, name, now, now, record_id))
                    resource_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute("""INSERT INTO card_versions
                        (resource_id,resource_type,name,content_json,content_fingerprint,
                         version_kind,source_record_id,created_at,is_current)
                        VALUES(?,?,?,?,?,?,?,?,1)""", (
                            resource_id, resource_type, name,
                            json.dumps(content, ensure_ascii=False), fingerprint,
                            "original", record_id, now))
                    version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute("UPDATE card_resources SET current_version_id=?,first_source_id=? WHERE id=?",
                                 (version_id, record_id, resource_id))
                else:
                    resource_id, _current_id = resource
                    exists = conn.execute("""SELECT id FROM card_versions
                        WHERE resource_id=? AND content_fingerprint=? LIMIT 1""",
                                         (resource_id, fingerprint)).fetchone()
                    if not exists:
                        conn.execute("""INSERT INTO card_versions
                            (resource_id,resource_type,name,content_json,content_fingerprint,
                             version_kind,source_record_id,created_at,is_current)
                            VALUES(?,?,?,?,?,?,?,?,0)""", (
                                resource_id, resource_type, name,
                                json.dumps(content, ensure_ascii=False), fingerprint,
                                "original", record_id, now))
                    conn.execute("UPDATE card_resources SET latest_source_id=?,updated_at=? WHERE id=?",
                                 (record_id, now, resource_id))
                _library_add_source(conn, resource_id, record_id, now)
        conn.commit()


def _library_row(conn, resource_type, resource_id, include_deleted=False):
    sql = """SELECT r.id,r.resource_type,r.name,r.current_version_id,
        r.first_source_id,r.latest_source_id,r.created_at,r.updated_at,r.deleted_at,
        r.cloned_from_id,v.content_json,v.version_kind,v.source_record_id,v.created_at
        FROM card_resources r LEFT JOIN card_versions v ON v.id=r.current_version_id
        WHERE r.resource_type=? AND r.id=?"""
    params = [resource_type, resource_id]
    if not include_deleted:
        sql += " AND r.deleted_at IS NULL"
    return conn.execute(sql, params).fetchone()


def _library_item(row, source_count=0):
    if not row:
        return None
    try:
        content = json.loads(row[10] or "{}")
    except Exception:
        content = {}
    return {
        "id": row[0], "type": row[1], "typeLabel": LIBRARY_LABELS.get(row[1], row[1]),
        "name": row[2], "currentVersionId": row[3], "firstSourceId": row[4],
        "latestSourceId": row[5], "createdAt": row[6], "updatedAt": row[7],
        "deletedAt": row[8], "clonedFromId": row[9], "content": content,
        "versionKind": row[11], "currentSourceId": row[12],
        "currentVersionAt": row[13], "sourceCount": source_count,
    }


def _library_detail(conn, resource_type, resource_id, include_deleted=False):
    row = _library_row(conn, resource_type, resource_id, include_deleted)
    if not row:
        return None
    count = conn.execute("SELECT COUNT(*) FROM card_resource_sources WHERE resource_id=?",
                         (resource_id,)).fetchone()[0]
    item = _library_item(row, count)
    versions = conn.execute("""SELECT id,version_kind,source_record_id,created_at,
        content_fingerprint,is_current,name FROM card_versions
        WHERE resource_id=? ORDER BY id DESC""", (resource_id,)).fetchall()
    item["versions"] = [{"id": x[0], "kind": x[1], "sourceId": x[2],
                          "createdAt": x[3], "fingerprint": x[4],
                          "isCurrent": bool(x[5]), "name": x[6]} for x in versions]
    return item


def _library_create(conn, resource_type, name, content, now=None,
                    cloned_from=None, source_id=None, kind="edited"):
    now = now or _library_now()
    name = str(name or "").strip()[:160] or LIBRARY_LABELS.get(resource_type, "资源")
    fp = _library_fingerprint(content)
    conn.execute("""INSERT INTO card_resources
        (resource_type,name,created_at,updated_at,latest_source_id,cloned_from_id)
        VALUES(?,?,?,?,?,?)""", (resource_type, name, now, now, source_id, cloned_from))
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("""INSERT INTO card_versions
        (resource_id,resource_type,name,content_json,content_fingerprint,
         version_kind,source_record_id,created_at,is_current)
        VALUES(?,?,?,?,?,?,?,?,1)""", (rid, resource_type, name,
        json.dumps(content, ensure_ascii=False), fp, kind, source_id, now))
    vid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE card_resources SET current_version_id=?,first_source_id=? WHERE id=?",
                 (vid, source_id, rid))
    if source_id:
        _library_add_source(conn, rid, source_id, now)
    return rid


def _validate_library_type(resource_type):
    return resource_type in LIBRARY_TYPES


def _library_packet(item):
    """Wrap one current resource in the existing chat-site import protocol."""
    if not item:
        return None
    content = item.get("content") or {}
    source = {"recordId": item.get("currentSourceId") or item.get("latestSourceId"),
              "model": (content.get("source") or {}).get("model", ""),
              "format": "library"}
    packet = {
        "type": "ai-card-catcher-import", "version": 1, "source": source,
        "characters": [], "personas": [], "jailbreaks": [], "lorebooks": [],
        "rules": [], "options": {"characterLorebookMode": "embedded",
                                   "globalLorebookMode": "global",
                                   "enableJailbreak": False,
                                   "rulesTarget": "character_system_prompt"},
    }
    if item["type"] == "character":
        packet["characters"] = [content]
    elif item["type"] == "player":
        packet["personas"] = [content]
    elif item["type"] == "jailbreak":
        packet["jailbreaks"] = [content]
    elif item["type"] == "worldbook":
        packet["lorebooks"] = [content]
    return packet


def access_token_key():
    name = CFG.get("access_token_name", "VSOUL专用")
    with appdb_conn() as conn:
        row = conn.execute("select key,status,unlimited_quota from tokens "
                           "where name=?", (name,)).fetchone()
    if row:
        return row[0], row[1]
    return "", None


def ensure_access_token():
    if not os.path.exists(APP_DB):
        return                                    # New API db not migrated yet
    name = CFG.get("access_token_name", "VSOUL专用")
    with appdb_conn() as conn:
        row = conn.execute("select id,status from tokens where name=?",
                           (name,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tokens (user_id,key,name,status,created_time,"
                "accessed_time,expired_time,remain_quota,unlimited_quota,"
                "model_limits_enabled) VALUES (1,?,?,1,?,?, -1,0,1,0)",
                (os.urandom(24).hex(), name, int(time.time()),
                 int(time.time())))
            conn.commit()


# ---------------------------------------------------------------- admin credentials
# The New API console (port 3000) needs an admin login. The root password is
# stored as an argon2 hash, which can never be recovered to plaintext. Instead
# of trying to "show" the existing password we take ownership of it:
#   1. generate a strong random password once (stored, plaintext, in config)
#   2. mint an argon2 hash for that password via New API's own register
#      endpoint (New API hashes it server-side; we copy the hash onto root,
#      then delete the throwaway account)
#   3. verify on every status poll: read root's hash from the sqlite db and
#      compare with the hash we saved. If it drifted (user changed it / reset
#      it / re-ran setup), re-apply our hash so the on-page credentials are
#      always correct ("real-time" self-healing).
#
# The saved hash in config is double-protected: hashing the password again
# would be useless (argon2 salts), so instead we save both plaintext password
# AND the exact hash, XOR-masked with a per-machine key derived from the
# session secret. Not bulletproof but far better than storing plaintext.

ADMIN_USER = "root"
_ADMIN_PROBE_NAME = "acc_throwaway"


def _admin_salt():
    """Per-machine obfuscation key derived from the session secret."""
    base = CFG.get("session_secret", "") or "aicardcatcher-secret"
    import hashlib
    return hashlib.sha256(base.encode("utf8")).digest()


def _xor_bytes(data, key):
    key = key * (len(data) // len(key) + 1)
    return bytes(a ^ b for a, b in zip(data, key))


def _admin_store(pw, hashed):
    """Save password + hash (XOR-masked with the machine key) to config."""
    key = _admin_salt()
    cfg = load_config()
    cfg["admin_credentials"] = {
        "username": ADMIN_USER,
        "password": _xor_bytes(pw.encode("utf8"), key).hex(),
        "hash": hashed,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_config(cfg)


def _admin_load():
    c = CFG.get("admin_credentials") or {}
    if not c.get("password") or not c.get("hash"):
        return None
    try:
        pw = _xor_bytes(bytes.fromhex(c["password"]), _admin_salt()).decode("utf8")
    except Exception:
        return None
    return {"username": c.get("username", ADMIN_USER), "password": pw,
            "hash": c.get("hash"), "updated": c.get("updated", "")}


def _root_hash():
    """Read root's password hash straight from New API's sqlite db."""
    if not os.path.exists(APP_DB):
        return None
    try:
        with appdb_conn() as conn:
            row = conn.execute(
                "select password from users where username=? and role=100",
                (ADMIN_USER,)).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _admin_reset_pw():
    """Reset root's password to the safe password. Returns (ok, message)."""
    if not os.path.exists(APP_DB):
        return False, "New API database not ready yet"
    # 1) make sure we have a stored password; create one if missing
    stored = _admin_load()
    if stored is None:
        pw = secrets.token_urlsafe(9) + "aA1!"          # strong & copyable
        # mint hash via register endpoint (New API hashes server-side)
        ok, hashed = _mint_hash(pw)
        if not ok:
            return False, hashed
        _admin_store(pw, hashed)
        stored = _admin_load()
    pw, hashed = stored["password"], stored["hash"]
    # 2) apply our hash to root (direct DB update, no HTTP needed)
    try:
        with appdb_conn() as conn:
            conn.execute("update users set password=? where username=?",
                         (hashed, ADMIN_USER))
            conn.commit()
        return True, "root 密码已由本工具托管"
    except Exception as e:
        return False, "update failed: %r" % (e,)


def _mint_hash(pw):
    """Have New API hash a password for us: register a throwaway user via the
    HTTP API, copy its argon2 hash, delete the user from the db.
    Returns (ok, hash_or_error)."""
    try:
        import urllib.request as _ur
        port = CFG["newapi_port"]
        body = json.dumps({"username": _ADMIN_PROBE_NAME,
                           "password": pw}).encode("utf8")
        req = _ur.Request("http://127.0.0.1:%d/api/user/register" % port,
                          data=body,
                          headers={"Content-Type": "application/json"})
        resp = _ur.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf8", "ignore"))
        if not data.get("success"):
            return False, "register failed: %s" % data.get("message", "?")
        with appdb_conn() as conn:
            row = conn.execute(
                "select id,password from users where username=?",
                (_ADMIN_PROBE_NAME,)).fetchone()
            if not row:
                return False, "throwaway user not found"
            hashed = row[1]
            conn.execute("delete from users where username=?",
                         (_ADMIN_PROBE_NAME,))
            conn.commit()
        if not hashed or not hashed.startswith("$"):
            return False, "unexpected hash: %r" % (hashed[:20],)
        return True, hashed
    except Exception as e:
        return False, "hash mint error: %r" % (e,)


def admin_credentials():
    """Called on every status poll. Ensures the safe password is applied to
    root and returns the credentials for the UI. Self-healing: if root's hash
    drifted from what we saved, re-apply it."""
    if not os.path.exists(APP_DB):
        return {"available": False, "message": "New API 数据库尚未初始化"}
    stored = _admin_load()
    if stored is None:
        ok, msg = _admin_reset_pw()
        if not ok:
            return {"available": False, "message": msg}
        stored = _admin_load()
    # compare current root hash with saved hash; heal if drifted
    cur = _root_hash()
    if cur != stored["hash"]:
        ok, msg = _admin_reset_pw()
        if not ok:
            return {"available": False, "message": msg,
                    "username": stored["username"], "password": stored["password"]}
        stored = _admin_load()
    return {"available": True,
            "username": stored["username"],
            "password": stored["password"],
            "updated": stored.get("updated", "")}


def mask_token(raw):
    raw = re.sub(r"(?i)^bearer\s+", "", raw or "").strip()
    return (raw[:8] + "...") if len(raw) > 12 else raw


# ---------------------------------------------------------------- forwarding
def forward_and_record(method, path, query, headers, body):
    """Forward a /v1 request to New API and record everything."""
    up_headers = {k: v for k, v in headers.items()
                  if k.lower() not in HOP_BY_HOP}
    up_headers["Host"] = "127.0.0.1:%d" % CFG["newapi_port"]
    up_headers["Content-Length"] = str(len(body))
    client_ip = headers.get("X-Real-IP", "127.0.0.1")

    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": method, "path": path + (("?" + query) if query else ""),
        "model": "", "token": mask_token(headers.get("Authorization")),
        "client_ip": client_ip, "stream": 0,
        "req_body": body.decode("utf8", "ignore")[:MAX_STORE],
    }
    try:
        obj = json.loads(rec["req_body"] or "{}")
        rec["model"] = obj.get("model", "") if isinstance(obj, dict) else ""
        rec["stream"] = 1 if isinstance(obj, dict) and obj.get("stream") else 0
    except Exception:
        pass

    url = "http://127.0.0.1:%d%s" % (CFG["newapi_port"], path)
    if query:
        url += "?" + query
    started = time.time()
    try:
        req = Request(url, data=body or None, headers=up_headers, method=method)
        resp = urlopen(req, timeout=UPSTREAM_TIMEOUT)
        # Keep the actual response object so GET /v1/models and all other
        # non-streaming responses are copied back to the client.
        status, rheaders, stream = resp.status, dict(resp.getheaders()), resp
    except HTTPError as e:
        status, rheaders, stream = e.code, dict(e.headers.items()), e
    except Exception as e:
        rec.update({"status": 0, "elapsed_ms": int((time.time() - started) * 1000),
                    "error": "newapi unreachable: %r" % (e,)})
        save_log(rec)
        msg = ("New API is not running. Start it from the config page."
               ).encode("utf8")
        return 502, {"Content-Type": "text/plain; charset=utf-8"}, msg, False

    ctype = (rheaders.get("Content-Type") or
             rheaders.get("content-type") or "")
    is_stream = "event-stream" in ctype.lower() or bool(rec["stream"])

    chunks = []
    try:
        while True:
            chunk = stream.read(READ_CHUNK) if not isinstance(stream, bytes) else b""
            if not chunk:
                break
            chunks.append(chunk)
    except Exception as e:
        rec["error"] = "relay error: %r" % (e,)

    raw = b"".join(chunks)
    rec["elapsed_ms"] = int((time.time() - started) * 1000)
    rec["status"] = status
    rec["resp_body"] = raw.decode("utf8", "ignore")[:MAX_STORE]
    rec["resp_text"] = _extract_reply(raw, is_stream)[:MAX_STORE]
    save_log(rec)
    rheaders = {k: v for k, v in rheaders.items()
                if k.lower() not in HOP_BY_HOP and k.lower() != "date"}
    return status, rheaders, raw, is_stream


def _extract_reply(body_bytes, streamed):
    if not body_bytes:
        return ""
    text = body_bytes.decode("utf8", "ignore")
    if streamed:
        out = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                obj = json.loads(payload)
                for ch in obj.get("choices", []):
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        out.append(d["content"])
                    elif ch.get("text"):
                        out.append(ch["text"])
            except Exception:
                continue
        return "".join(out)
    try:
        obj = json.loads(text)
        parts = []
        for ch in obj.get("choices", []):
            m = ch.get("message") or {}
            if m.get("content"):
                parts.append(m["content"])
            elif ch.get("text"):
                parts.append(ch["text"])
        return "\n".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------- web ui
def load_index():
    p = os.path.join(WEB, "index.html")
    if os.path.exists(p):
        return open(p, "rb").read()
    return b"<h1>web/index.html missing</h1>"


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AI-Card-Catcher"

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self):
        tok = CFG.get("view_token", "")
        if not tok:
            return True
        qs = parse_qs(urlparse(self.path).query)
        if qs.get("token", [""])[0] == tok:
            return True
        if self.headers.get("X-CC-Token") == tok:
            return True
        self._json({"error": "unauthorized"}, 401)
        return False

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send_bytes(self, data, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # ---------- verbs
    # NOTE: each verb MUST be its own def - "do_POST = do_GET" would alias
    # the function and route every POST with method="GET", silently breaking
    # all POST admin endpoints (start/stop/config/shutdown...).
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def do_PATCH(self):
        self.route("PATCH")

    def do_OPTIONS(self):
        self.route("OPTIONS")

    def route(self, method):
        u = urlparse(self.path)
        path, query = u.path, u.query
        try:
            if path.startswith("/v1/"):
                self._proxy(method, path, query)
            elif path.startswith("/cc/api/"):
                if not self._guard():
                    return
                self._admin(method, path, query)
            elif path == "/" or path == "/index.html":
                data = load_index()
                self._send_bytes(data, "text/html; charset=utf-8")
            elif path == "/cc/logo.svg":
                self._send_bytes(b"", "image/svg+xml")
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": repr(e)}, 500)
            except Exception:
                pass

    # ---------- proxy
    def _proxy(self, method, path, query):
        body = self._read_body()
        headers = dict(self.headers.items())
        status, rheaders, raw, is_stream = forward_and_record(
            method, path, query, headers, body)
        try:
            self.send_response(status)
            for k, v in rheaders.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- admin api
    def _admin(self, method, path, query):
        action = path[len("/cc/api/"):]

        if action == "shutdown" and method == "POST":
            # reply first, then tear everything down in a background thread
            self._json({"ok": True,
                        "message": "全部服务正在关闭，可关闭本页面；重新启动请双击 start.bat"})
            threading.Timer(0.5, shutdown_everything).start()
            return

        if action == "status" and method == "GET":
            self._json(self._status())
            return

        if action.startswith("service/") and method == "POST":
            parts = action.split("/")            # service/newapi/start
            if len(parts) != 3:
                self._json({"error": "bad action"}, 400)
                return
            svc, op = parts[1], parts[2]
            fn = {"newapi": {"start": start_newapi, "stop": stop_newapi},
                  "cpolar": {"start": start_cpolar, "stop": stop_cpolar}}
            if svc not in fn or op not in fn[svc]:
                self._json({"error": "unknown service"}, 400)
                return
            ok, msg = fn[svc][op]()
            time.sleep(1.0)
            self._json({"ok": ok, "message": msg, "status": self._status()})
            return

        if action == "config" and method == "GET":
            cfg = json.loads(json.dumps(CFG))
            if cfg.get("cpolar", {}).get("authtoken"):
                a = cfg["cpolar"]["authtoken"]
                cfg["cpolar"]["authtoken_masked"] = a[:6] + "..." + a[-4:]
                cfg["cpolar"]["authtoken"] = ""
            cfg["session_secret"] = ""
            self._json({"config": cfg})
            return

        if action == "config" and method == "POST":
            body = json.loads(self._read_body().decode("utf8") or "{}")
            with CONFIG_LOCK:
                for key in ("cpolar_region",):
                    pass
                if "cpolar_authtoken" in body and body["cpolar_authtoken"].strip():
                    CFG["cpolar"]["authtoken"] = body["cpolar_authtoken"].strip()
                if "cpolar_region" in body:
                    CFG["cpolar"]["region"] = body["cpolar_region"]
                if "auto_start_cpolar" in body:
                    CFG["auto_start_cpolar"] = bool(body["auto_start_cpolar"])
                if "auto_start_newapi" in body:
                    CFG["auto_start_newapi"] = bool(body["auto_start_newapi"])
                if "access_token_name" in body and body["access_token_name"].strip():
                    CFG["access_token_name"] = body["access_token_name"].strip()
                if "classify_rules" in body:
                    cr = body["classify_rules"]
                    if isinstance(cr, dict):
                        CFG["classify_rules"] = cr
                save_config(CFG)
            self._json({"ok": True})
            return

        if action == "test/newapi" and method == "POST":
            try:
                r = urlopen("http://127.0.0.1:%d/api/status" %
                            CFG["newapi_port"], timeout=8)
                d = json.loads(r.read().decode("utf8"))
                self._json({"ok": True, "message":
                            "New API online, version " +
                            str(d.get("data", {}).get("version", "?"))})
            except Exception as e:
                self._json({"ok": False, "message": "New API unreachable: %r" % (e,)})
            return

        if action == "test/public" and method == "POST":
            pub = read_public_url()
            if not pub:
                self._json({"ok": False, "message": "Tunnel not established"})
                return
            try:
                r = urlopen(pub + "/v1/models", timeout=20)
                self._json({"ok": True, "message":
                            "Public URL alive: %s" % pub, "url": pub})
            except HTTPError as e:
                ok = e.code in (401, 403)
                self._json({"ok": ok, "message":
                            "%s -> HTTP %d (%s)" % (pub, e.code,
                            "reachable, needs key" if ok else "error"),
                            "url": pub})
            except Exception as e:
                self._json({"ok": False, "message":
                            "Public URL failed: %r" % (e,), "url": pub})
            return

        if action == "access" and method == "GET":
            key, status = access_token_key()
            self._json({"api_url": self._public_v1(), "key": key,
                        "key_status": status, "models": self._models(),
                        "public_url": read_public_url()})
            return

        # 联动导入配置：默认允许本机/局域网聊天网站地址，实际发送仍由
        # 用户在页面点击，避免后台静默把抓取内容推到未知地址。
        if action == "link/config" and method == "GET":
            self._json({"chat_url": CFG.get("link_chat_url", "http://127.0.0.1:5173")})
            return
        if action == "link/config" and method == "POST":
            body = json.loads(self._read_body().decode("utf8") or "{}")
            value = str(body.get("chat_url", "")).strip().rstrip("/")
            if not value.startswith(("http://", "https://")):
                self._json({"ok": False, "message": "聊天网站地址必须以 http:// 或 https:// 开头"}, 400)
                return
            with CONFIG_LOCK:
                CFG["link_chat_url"] = value
                save_config(CFG)
            self._json({"ok": True, "chat_url": value})
            return

        m = re.match(r"^link/packet/(\d+)$", action)
        if m and method == "GET":
            rid = int(m.group(1))
            with _db_lock, db_conn() as conn:
                row = conn.execute("""select r.req_body,r.model from request_logs r
                                   left join deleted_records d on d.id=r.id
                                   where r.id=? and d.id is null""", (rid,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                req = json.loads(row[0] or "{}")
                fmt, sections = classify_request(req)
                packet = build_link_import(sections, row[1] or "", rid)
                packet["source"]["format"] = fmt
                self._json(packet)
            except Exception as e:
                self._json({"error": "req_body unparseable: %r" % (e,)}, 400)
            return

        m = re.match(r"^link/packet/(\d+)/export$", action)
        if m and method == "GET":
            rid = int(m.group(1))
            with _db_lock, db_conn() as conn:
                row = conn.execute("""select r.req_body,r.model from request_logs r
                                   left join deleted_records d on d.id=r.id
                                   where r.id=? and d.id is null""", (rid,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                req = json.loads(row[0] or "{}")
                fmt, sections = classify_request(req)
                packet = build_link_import(sections, row[1] or "", rid)
                packet["source"]["format"] = fmt
                payload = json.dumps(packet, ensure_ascii=False, indent=2).encode("utf8")
                self._send_bytes(payload, "application/json; charset=utf-8", extra={
                    "Content-Disposition": 'attachment; filename="ai-card-catcher-%d.json"' % rid
                })
            except Exception as e:
                self._json({"error": "req_body unparseable: %r" % (e,)}, 400)
            return

        if action == "link/push" and method == "POST":
            body = json.loads(self._read_body().decode("utf8") or "{}")
            packet = body.get("packet")
            target = str(body.get("chat_url") or CFG.get("link_chat_url", "")).strip().rstrip("/")
            if not isinstance(packet, dict) or packet.get("type") != "ai-card-catcher-import":
                self._json({"ok": False, "message": "联动数据包格式不正确"}, 400)
                return
            if not target.startswith(("http://", "https://")):
                self._json({"ok": False, "message": "聊天网站地址不正确"}, 400)
                return
            try:
                raw = json.dumps(packet, ensure_ascii=False).encode("utf8")
                req = Request(target + "/link-import/receive", data=raw,
                              headers={"Content-Type": "application/json", "X-AI-Card-Catcher": "1"})
                with urlopen(req, timeout=20) as response:
                    reply = response.read().decode("utf8", "ignore")
                try:
                    result = json.loads(reply or "{}")
                except Exception:
                    result = {"ok": response.status < 300, "message": reply[:500]}
                result["target"] = target
                self._json(result, 200 if response.status < 300 else response.status)
            except HTTPError as e:
                detail = e.read().decode("utf8", "ignore")[:500]
                self._json({"ok": False, "message": "聊天网站返回 HTTP %d: %s" % (e.code, detail)}, 502)
            except Exception as e:
                self._json({"ok": False, "message": "无法连接聊天网站：%r" % (e,)}, 502)
            return

        # ---------------- resource card library ----------------
        if action == "library" and method == "GET":
            sync_card_library()
            qs = parse_qs(query)
            resource_type = (qs.get("type", [""])[0] or "").strip()
            keyword = (qs.get("keyword", [""])[0] or "").strip().lower()
            include_deleted = qs.get("trash", [""])[0] in ("1", "true", "yes")
            sort = (qs.get("sort", ["updated"])[0] or "updated").strip()
            order = "r.updated_at DESC, r.id DESC"
            if sort == "created":
                order = "r.created_at DESC, r.id DESC"
            elif sort == "sources":
                order = "source_count DESC, r.updated_at DESC, r.id DESC"
            where = []
            params = []
            if resource_type:
                if not _validate_library_type(resource_type):
                    self._json({"error": "unknown resource type"}, 400)
                    return
                where.append("r.resource_type=?")
                params.append(resource_type)
            where.append("r.deleted_at IS " + ("NOT NULL" if include_deleted else "NULL"))
            sql = """SELECT r.id,r.resource_type,r.name,r.current_version_id,
                r.first_source_id,r.latest_source_id,r.created_at,r.updated_at,
                r.deleted_at,r.cloned_from_id,v.content_json,v.version_kind,
                v.source_record_id,v.created_at,
                (SELECT COUNT(*) FROM card_resource_sources s WHERE s.resource_id=r.id) AS source_count
                FROM card_resources r LEFT JOIN card_versions v ON v.id=r.current_version_id
                WHERE """ + " AND ".join(where)
            with _db_lock, db_conn() as conn:
                rows = conn.execute(sql + " ORDER BY " + order, params).fetchall()
                items = []
                for row in rows:
                    item = _library_item(row[:14], row[14])
                    haystack = (item["name"] + " " + json.dumps(item["content"], ensure_ascii=False)).lower()
                    if keyword and keyword not in haystack:
                        continue
                    items.append(item)
            if sort == "sources":
                items.sort(key=lambda x: (x.get("sourceCount", 0), x.get("updatedAt", "")), reverse=True)
            self._json({"items": items, "total": len(items), "types": LIBRARY_LABELS,
                        "trash": include_deleted})
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)$", action)
        if m and method == "GET":
            sync_card_library()
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                item = _library_detail(conn, resource_type, resource_id)
            if not item:
                self._json({"error": "resource not found"}, 404)
            else:
                self._json(item)
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)/clone$", action)
        if m and method == "POST":
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                source = _library_detail(conn, resource_type, resource_id)
                if not source:
                    self._json({"error": "resource not found"}, 404)
                    return
                body = json.loads(self._read_body().decode("utf8") or "{}")
                new_name = str(body.get("name") or (source["name"] + " - 副本"))[:160]
                origin_source = source.get("currentSourceId") or source.get("latestSourceId")
                new_id = _library_create(conn, resource_type, new_name,
                                         source["content"], cloned_from=resource_id,
                                         source_id=origin_source, kind="original")
                conn.commit()
                item = _library_detail(conn, resource_type, new_id)
            self._json({"ok": True, "item": item})
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)$", action)
        if m and method == "PUT":
            resource_type, resource_id = m.group(1), int(m.group(2))
            body = json.loads(self._read_body().decode("utf8") or "{}")
            content = body.get("content")
            if not isinstance(content, dict):
                self._json({"error": "content must be an object"}, 400)
                return
            with _db_lock, db_conn() as conn:
                source = _library_detail(conn, resource_type, resource_id)
                if not source:
                    self._json({"error": "resource not found"}, 404)
                    return
                name = str(body.get("name") or source["name"]).strip()[:160] or source["name"]
                now = _library_now()
                conn.execute("UPDATE card_versions SET is_current=0 WHERE resource_id=?", (resource_id,))
                conn.execute("""INSERT INTO card_versions
                    (resource_id,resource_type,name,content_json,content_fingerprint,
                     version_kind,source_record_id,created_at,is_current)
                    VALUES(?,?,?,?,?,?,?,?,1)""", (
                        resource_id, resource_type, name, json.dumps(content, ensure_ascii=False),
                        _library_fingerprint(content), "edited", None, now))
                version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute("""UPDATE card_resources SET name=?,current_version_id=?,updated_at=?
                    WHERE id=?""", (name, version_id, now, resource_id))
                conn.commit()
                item = _library_detail(conn, resource_type, resource_id)
            self._json({"ok": True, "item": item})
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)$", action)
        if m and method == "DELETE":
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                row = _library_row(conn, resource_type, resource_id)
                if not row:
                    self._json({"error": "resource not found"}, 404)
                    return
                now = _library_now()
                conn.execute("UPDATE card_resources SET deleted_at=?,updated_at=? WHERE id=?",
                             (now, now, resource_id))
                conn.commit()
            self._json({"ok": True, "message": "资源已移入卡片库回收站"})
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)/restore$", action)
        if m and method == "POST":
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                row = _library_row(conn, resource_type, resource_id)
                if not row:
                    self._json({"error": "resource not found"}, 404)
                    return
                original = conn.execute("""SELECT id,name,content_json,source_record_id
                    FROM card_versions WHERE resource_id=? AND version_kind='original'
                    ORDER BY id DESC LIMIT 1""", (resource_id,)).fetchone()
                if not original:
                    self._json({"error": "没有可还原的原始抓取版本"}, 400)
                    return
                now = _library_now()
                conn.execute("UPDATE card_versions SET is_current=0 WHERE resource_id=?", (resource_id,))
                conn.execute("""INSERT INTO card_versions
                    (resource_id,resource_type,name,content_json,content_fingerprint,
                     version_kind,source_record_id,created_at,is_current)
                    VALUES(?,?,?,?,?,?,?,?,1)""", (
                        resource_id, resource_type, original[1], original[2],
                        _library_fingerprint(json.loads(original[2] or "{}")), "restore",
                        original[3], now))
                vid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute("UPDATE card_resources SET name=?,current_version_id=?,updated_at=? WHERE id=?",
                             (original[1], vid, now, resource_id))
                conn.commit()
                item = _library_detail(conn, resource_type, resource_id)
            self._json({"ok": True, "item": item, "message": "已还原到最近一次原始抓取版本"})
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)/undelete$", action)
        if m and method == "POST":
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                cur = conn.execute("UPDATE card_resources SET deleted_at=NULL,updated_at=? WHERE resource_type=? AND id=?",
                                   (_library_now(), resource_type, resource_id))
                conn.commit()
            self._json({"ok": bool(cur.rowcount), "message": "已恢复卡片" if cur.rowcount else "资源不存在"},
                        200 if cur.rowcount else 404)
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)/sources$", action)
        if m and method == "GET":
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                exists = _library_row(conn, resource_type, resource_id, True)
                rows = conn.execute("""SELECT r.id,r.ts,r.model,r.status,r.path,
                    CASE WHEN d.id IS NULL THEN 0 ELSE 1 END
                    FROM card_resource_sources s JOIN request_logs r ON r.id=s.source_record_id
                    LEFT JOIN deleted_records d ON d.id=r.id
                    WHERE s.resource_id=? ORDER BY r.id DESC""", (resource_id,)).fetchall()
            if not exists:
                self._json({"error": "resource not found"}, 404)
                return
            self._json({"items": [{"id": x[0], "ts": x[1], "model": x[2],
                                   "status": x[3], "path": x[4], "deleted": bool(x[5])}
                                  for x in rows]})
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)/export$", action)
        if m and method == "GET":
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                item = _library_detail(conn, resource_type, resource_id)
            if not item:
                self._json({"error": "resource not found"}, 404)
                return
            payload = json.dumps(item["content"], ensure_ascii=False, indent=2).encode("utf8")
            self._send_bytes(payload, "application/json; charset=utf-8", extra={
                "Content-Disposition": 'attachment; filename="card-%s-%d.json"' % (resource_type, resource_id)
            })
            return

        m = re.match(r"^library/(character|jailbreak|worldbook|player)/(\d+)/link$", action)
        if m and method == "GET":
            resource_type, resource_id = m.group(1), int(m.group(2))
            with _db_lock, db_conn() as conn:
                item = _library_detail(conn, resource_type, resource_id)
            if not item:
                self._json({"error": "resource not found"}, 404)
                return
            self._json(_library_packet(item))
            return

        if action == "library" and method == "POST":
            body = json.loads(self._read_body().decode("utf8") or "{}")
            resource_type = str(body.get("type") or "").strip()
            content = body.get("content")
            if not _validate_library_type(resource_type) or not isinstance(content, dict):
                self._json({"error": "type or content invalid"}, 400)
                return
            with _db_lock, db_conn() as conn:
                rid = _library_create(conn, resource_type, body.get("name"), content)
                conn.commit()
                item = _library_detail(conn, resource_type, rid)
            self._json({"ok": True, "item": item}, 201)
            return

        if action == "records" and method == "GET":
            qs = parse_qs(query)
            page = max(1, int(qs.get("page", ["1"])[0] or 1))
            page_size = min(max(10, int((qs.get("page_size") or qs.get("limit") or ["50"])[0] or 50)), 200)
            offset = (page - 1) * page_size
            kw = (qs.get("keyword", [""])[0] or "").strip()
            status_filter = (qs.get("status", [""])[0] or "").strip()
            fmt_filter = (qs.get("format", [""])[0] or "").strip()
            content_filter = (qs.get("content", [""])[0] or "").strip()
            where = ["d.id IS NULL"]
            params = []
            if kw:
                where.append("(r.req_body LIKE ? OR r.resp_text LIKE ? OR r.model LIKE ?"
                             " OR r.token LIKE ? OR r.system_prompt LIKE ?)")
                like = "%" + kw + "%"
                params += [like] * 5
            if status_filter == "success":
                where.append("r.status >= 200 AND r.status < 300")
            elif status_filter == "error":
                where.append("(r.status < 200 OR r.status >= 300)")
            elif status_filter.isdigit():
                where.append("r.status = ?")
                params.append(int(status_filter))
            sql = ("SELECT r.id,r.ts,r.path,r.model,r.token,r.status,r.elapsed_ms,"
                   "length(r.req_body),length(r.resp_body),r.resp_text,r.system_prompt,r.req_body "
                   "FROM request_logs r LEFT JOIN deleted_records d ON d.id=r.id "
                   "WHERE " + " AND ".join(where))
            all_rows = []
            with _db_lock, db_conn() as conn:
                all_rows = list(conn.execute(sql + " ORDER BY r.id DESC", params))
            items = []
            for r in all_rows:
                rec_fmt = "unknown"
                summary = {}
                try:
                    req = json.loads(r[11] or "{}")
                    rec_fmt, sections = classify_request(req)
                    for sec in sections:
                        summary[sec["type"]] = summary.get(sec["type"], 0) + sec["length"]
                except Exception:
                    pass
                if fmt_filter and rec_fmt != fmt_filter:
                    continue
                if content_filter:
                    has_content = content_filter in summary
                    if content_filter == "jailbreak":
                        has_content = has_content or "nsfw_guide" in summary
                    if not has_content:
                        continue
                items.append({
                    "id": r[0], "ts": r[1], "path": r[2], "model": r[3],
                    "token": r[4], "status": r[5], "elapsed_ms": r[6],
                    "req_size": r[7] or 0, "resp_size": r[8] or 0,
                    "reply": (r[9] or "")[:500], "has_system": bool(r[10]),
                    "format": rec_fmt, "summary": summary,
                })
            total = len(items)
            page_items = items[offset:offset + page_size]
            stats = {
                "total": total,
                "success": sum(1 for x in items if x["status"] >= 200 and x["status"] < 300),
                "error": sum(1 for x in items if x["status"] < 200 or x["status"] >= 300),
                "bytes": sum(x["req_size"] or 0 for x in items),
                "page": page,
                "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size),
            }
            self._json({"items": page_items, "total": total, "stats": stats})
            return

        if action == "records/stats" and method == "GET":
            with _db_lock, db_conn() as conn:
                row = conn.execute("""SELECT
                    COUNT(r.id),
                    SUM(CASE WHEN r.status >= 200 AND r.status < 300 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN r.status < 200 OR r.status >= 300 THEN 1 ELSE 0 END),
                    COALESCE(SUM(length(r.req_body)),0),
                    COALESCE(SUM(length(r.resp_body)),0)
                    FROM request_logs r LEFT JOIN deleted_records d ON d.id=r.id
                    WHERE d.id IS NULL""").fetchone()
                trash = conn.execute("SELECT COUNT(*) FROM deleted_records").fetchone()[0]
                content_rows = conn.execute("""SELECT r.req_body
                    FROM request_logs r LEFT JOIN deleted_records d ON d.id=r.id
                    WHERE d.id IS NULL AND r.req_body IS NOT NULL
                    ORDER BY r.id DESC""").fetchall()

            # Count records containing each content category, plus total chars.
            # A single request counts once per category even if it has multiple
            # messages of that type; chars/sections preserve the actual volume.
            content = {
                "character": {"records": 0, "sections": 0, "chars": 0},
                "jailbreak": {"records": 0, "sections": 0, "chars": 0},
                "worldbook": {"records": 0, "sections": 0, "chars": 0},
                "player": {"records": 0, "sections": 0, "chars": 0},
            }
            for (raw,) in content_rows:
                try:
                    req = json.loads(raw or "{}")
                    _, sections = classify_request(req)
                except Exception:
                    continue
                seen = set()
                for sec in sections:
                    kind = sec.get("type")
                    target = kind
                    # 描写指导属于破甲内容，合并到破甲统计。
                    if kind == "nsfw_guide":
                        target = "jailbreak"
                    if target not in content:
                        continue
                    content[target]["sections"] += 1
                    content[target]["chars"] += sec.get("length", 0)
                    seen.add(target)
                for target in seen:
                    content[target]["records"] += 1

            self._json({"total": row[0] or 0, "success": row[1] or 0,
                        "error": row[2] or 0, "req_bytes": row[3] or 0,
                        "resp_bytes": row[4] or 0, "trash": trash,
                        "content": content})
            return

        m = re.match(r"^records/(\d+)$", action)
        if m and method == "DELETE":
            ok, result = soft_delete_records([m.group(1)])
            self._json({"ok": ok, "deleted": result if ok else [],
                        "message": "已移入回收站" if ok else result}, 200 if ok else 400)
            return
        if m and method == "GET":
            rid = int(m.group(1))
            with _db_lock, db_conn() as conn:
                row = conn.execute(
                    """select r.id,r.ts,r.path,r.model,r.token,r.status,r.elapsed_ms,
                    r.req_body,r.resp_body,r.resp_text,r.error
                    from request_logs r left join deleted_records d on d.id=r.id
                    where r.id=? and d.id is null""",
                    (rid,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                req = json.loads(row[7] or "{}")
                fmt, pairs = unified_messages(req)
                msgs = [{"role": r, "content": t} for r, t in pairs]
            except Exception:
                msgs = []
                req = {"_raw": (row[7] or "")[:5000]}
                fmt = "unknown"
            out = {
                "id": row[0], "ts": row[1], "path": row[2], "model": row[3],
                "token": row[4], "status": row[5], "elapsed_ms": row[6],
                "resp_text": (row[9] or "")[:8000], "error": row[10] or "",
                "format": fmt,
                "params": {k: v for k, v in req.items()
                           if k != "messages" and not isinstance(v, (list, dict))},
                "messages": [{"index": i, "role": mm["role"],
                              "content": mm["content"],
                              "length": len(mm["content"])}
                             for i, mm in enumerate(msgs)],
            }
            self._json(out)
            return

        m = re.match(r"^cards/(\d+)$", action)
        if m and method == "GET":
            rid = int(m.group(1))
            with _db_lock, db_conn() as conn:
                row = conn.execute("""select r.req_body,r.model from request_logs r
                                   left join deleted_records d on d.id=r.id
                                   where r.id=? and d.id is null""", (rid,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                req = json.loads(row[0] or "{}")
                fmt, sections = classify_request(req)
            except Exception as e:
                self._json({"error": "req_body unparseable: %r" % (e,),
                            "truncated_hint": "record may predate the size fix"},
                           200)
                return
            grouped = {}
            for s in sections:
                grouped.setdefault(s["type"], []).append(s)
            self._json({"id": rid, "model": row[1], "format": fmt,
                        "sections": sections,
                        "types": {k: {"label": TYPE_CN.get(k, k),
                                      "count": len(v),
                                      "total_chars": sum(x["length"] for x in v)}
                                  for k, v in grouped.items()}})
            return

        if action == "records/batch" and method == "POST":
            body = json.loads(self._read_body().decode("utf8") or "{}")
            op = body.get("op", "delete")
            ids = body.get("ids", [])
            if op == "delete":
                ok, result = soft_delete_records(ids)
            elif op == "restore":
                ok, result = restore_records(ids)
            elif op == "purge":
                ok, result = purge_records(ids)
            else:
                ok, result = False, "未知操作"
            self._json({"ok": ok, "ids": result if ok else [],
                        "message": "操作完成" if ok else result}, 200 if ok else 400)
            return

        if action == "trash" and method == "GET":
            with _db_lock, db_conn() as conn:
                rows = conn.execute("""SELECT r.id,r.ts,r.model,r.status,
                    r.req_body,d.deleted_at FROM deleted_records d
                    LEFT JOIN request_logs r ON r.id=d.id
                    ORDER BY d.deleted_at DESC""").fetchall()
            self._json({"items": [{"id": r[0], "ts": r[1], "model": r[2],
                                    "status": r[3], "req_size": len(r[4] or ""),
                                    "deleted_at": r[5]} for r in rows]})
            return

        if action == "backup" and method == "POST":
            ok, result = backup_records_db()
            self._json({"ok": ok, "path": result if ok else "",
                        "message": "备份已创建" if ok else result}, 200 if ok else 500)
            return

        m = re.match(r"^cards/(\d+)/export$", action)
        if m and method == "GET":
            rid = int(m.group(1))
            export_fmt = parse_qs(query).get("fmt", ["st"])[0]
            with _db_lock, db_conn() as conn:
                row = conn.execute("""select r.req_body,r.model from request_logs r
                                   left join deleted_records d on d.id=r.id
                                   where r.id=? and d.id is null""", (rid,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                req = json.loads(row[0] or "{}")
                fmt, sections = classify_request(req)
            except Exception as e:
                self._json({"error": "req_body unparseable: %r" % (e,)}, 200)
                return
            if export_fmt == "jailbreak":
                data = build_jailbreak(sections, row[1] or "", rid)
                fname = "jailbreak_%d.json" % rid
            elif fmt == "vsoul":
                data = build_vsoul(sections, row[1] or "")
                fname = "vsoul_card_%d.json" % rid
            elif fmt == "worldbook":
                data = build_worldbook(sections, row[1] or "")
                fname = "worldbook_%d.json" % rid
            elif fmt == "raw":
                data = req
                fname = "raw_request_%d.json" % rid
            else:
                data = build_st_v2(sections, row[1] or "")
                fname = "st_card_%d.json" % rid
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf8")
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % fname)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self._json({"error": "unknown action: %s" % action}, 404)

    def _count(self):
        with _db_lock, db_conn() as conn:
            return conn.execute("""SELECT COUNT(*) FROM request_logs r
                LEFT JOIN deleted_records d ON d.id=r.id WHERE d.id IS NULL""").fetchone()[0]

    def _models(self):
        try:
            if not os.path.exists(APP_DB):
                return []
            with appdb_conn() as conn:
                ms = []
                for r in conn.execute("select models from channels where status=1"):
                    ms += [x.strip() for x in (r[0] or "").split(",") if x.strip()]
                return ms
        except Exception:
            return []

    def _public_v1(self):
        pub = read_public_url()
        return (pub + "/v1") if pub else ""

    def _status(self):
        # idempotent: create the access token once New API's db exists
        # (on first run New API creates its db AFTER us, so main()'s
        #  ensure_access_token() call runs too early and must be retried here)
        try:
            ensure_access_token()
        except Exception:
            pass
        key, kstatus = access_token_key()
        return {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "newapi": {"running": newapi_running() or port_open(CFG["newapi_port"]),
                       "port": CFG["newapi_port"]},
            "admin": admin_credentials(),
            "cpolar": {"running": cpolar_running(),
                       "public_url": read_public_url(),
                       "token_set": bool(CFG.get("cpolar", {}).get("authtoken")),
                       "region": CFG.get("cpolar", {}).get("region", "cn")},
            "recorder": {"port": CFG["recorder_port"],
                         "total_records": self._count()},
            "access": {"key": key, "key_status": kstatus,
                       "models": self._models()},
        }


# ---------------------------------------------------------------- main
def main():
    init_db()
    migrate_old_records()
    try:
        ok, backup_path = backup_records_db()
        print("[backup] %s" % (backup_path if ok else backup_path))
    except Exception as e:
        print("[backup] failed: %r" % (e,))
    ensure_access_token()
    with open(os.path.join(DATA, "main.pid"), "w") as f:
        f.write(str(os.getpid()))

    # migrate an existing New API database from a sibling folder (upgrade path)
    newapi_db = os.path.join(BASE, "one-api.db")
    if not os.path.exists(newapi_db):
        old = os.path.join(os.path.dirname(BASE), "one-api.db")
        if os.path.exists(old):
            shutil.copy(old, newapi_db)
            for ext in ("-wal", "-shm"):
                if os.path.exists(old + ext):
                    shutil.copy(old + ext, newapi_db + ext)
            print("[migrate] copied existing New API database")

    if CFG.get("auto_start_newapi", True):
        ok, msg = start_newapi()
        print("[newapi] %s" % msg)
    if CFG.get("auto_start_cpolar", True) and CFG.get("cpolar", {}).get("authtoken"):
        ok, msg = start_cpolar()
        print("[cpolar] %s" % msg)

    port = CFG["recorder_port"]
    # 局域网联动：聊天网站可从同一局域网访问管理/代理服务。
    # 仍由 X-CC-Token 保护 /cc/api，代理 /v1 继续遵循 New API 令牌鉴权。
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    globals()["MAIN_SERVER"] = srv
    pub = read_public_url()

    print("=" * 64)
    print("  AI-Card-Catcher")
    print("  UI        : http://localhost:%d/?token=%s" %
          (port, CFG["view_token"]))
    print("  Proxy     : /v1/*  ->  New API :%d" % CFG["newapi_port"])
    print("  Public    : %s" % (pub or "(tunnel not started yet)"))
    print("  Database  : %s" % DB_PATH)
    print("  Stop      : run stop.bat (or close this window)")
    print("=" * 64)

    if CFG.get("open_browser", True):
        threading.Timer(1.5, lambda: webbrowser.open(
            "http://localhost:%d/?token=%s" % (port, CFG["view_token"]))).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_newapi()
        stop_cpolar()


if __name__ == "__main__":
    main()
