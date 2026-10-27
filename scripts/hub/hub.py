#!/usr/bin/env python3
"""Pod内で動くメインページ（8000番）。中身はPodの中だけで扱い、外のサービスには送らない。

- 結果：/workspace/*-lab/generated/<実行>/ の画像・音声を一覧にし、見る・聞く・評価する
  （評価は /workspace/hub/ratings.json に保存）
- 確認：ログの末尾、容量の内訳
- 作業：/workspace/hub/tasks/*.sh を、決めた入力欄だけでボタンから実行する
  （スクリプトの先頭に "# title: 名前" "# desc: 説明" "# param: 変数名 | 表示名 | text か textarea"。
    入力値は環境変数 HUB_<変数名> で渡す）

パスワードは環境変数 HUB_PASSWORD、無ければ FILEBROWSER_PASSWORD。標準ライブラリだけで動く。
"""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("HUB_ROOT", "/workspace"))
HUB = ROOT / "hub"
PORT = int(os.environ.get("HUB_PORT", "8000"))
PASSWORD = os.environ.get("HUB_PASSWORD") or os.environ.get("FILEBROWSER_PASSWORD") or ""
if not PASSWORD:
    PASSWORD = secrets.token_urlsafe(15)
    print(f"[hub] パスワードが未設定のため、今回だけのパスワードを作りました: {PASSWORD}", flush=True)
SESSION = hmac.new(PASSWORD.encode(), b"hub-session", hashlib.sha256).hexdigest()

AUDIO = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".webm"}
IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
LOCK = threading.Lock()
DISK = {"running": False, "at": None, "rows": []}


def labs():
    return sorted(p for p in ROOT.glob("*-lab") if p.is_dir())


def inside(path, base):
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def tail(path, nbytes=65536, lines=120):
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - nbytes))
        text = f.read().decode("utf-8", "replace")
    # tqdm の進捗（\r で上書きする行）は最後の状態だけ残す
    out = [l.rsplit("\r", 1)[-1] for l in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(l for l in out if l.strip())[-nbytes:].split("\n")[-lines:]


# ---------------- 結果 ----------------

def list_runs():
    runs = []
    for lab in labs():
        gen = lab / "generated"
        if not gen.is_dir():
            continue
        for d in gen.iterdir():
            if d.is_dir():
                runs.append({"lab": lab.name, "run": d.name, "mtime": d.stat().st_mtime})
    return sorted(runs, key=lambda r: -r["mtime"])


def run_dir(lab, run):
    d = ROOT / lab / "generated" / run
    if not re.fullmatch(r"[\w.-]+-lab", lab) or "/" in run or not inside(d, ROOT / lab / "generated") or not d.is_dir():
        raise FileNotFoundError(run)
    return d


def sentences_for(d):
    """実行フォルダか、その上の setup/ にある sentences.tsv（タグ・指示・本文）を読む"""
    for p in [d / "sentences.tsv", d.parent.parent / "setup" / "compare_sentences.tsv"]:
        if p.is_file():
            rows = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                cols = line.split("\t")
                if len(cols) >= 3:
                    rows[cols[0]] = {"caption": cols[1], "text": cols[2]}
            return rows
    return {}


def run_detail(lab, run):
    d = run_dir(lab, run)
    conds = {}
    for f in sorted(d.rglob("*")):
        ext = f.suffix.lower()
        if not f.is_file() or (ext not in AUDIO and ext not in IMAGE):
            continue
        rel = f.relative_to(d)
        cond = str(rel.parent) if str(rel.parent) != "." else "（直下）"
        conds.setdefault(cond, []).append({
            "name": f.name, "path": str(rel), "kind": "audio" if ext in AUDIO else "image",
        })
    return {"lab": lab, "run": run, "conditions": conds, "sentences": sentences_for(d)}


# ---------------- 確認 ----------------

def list_logs():
    files = []
    for base in [ROOT / "logs"] + [lab / "logs" for lab in labs()]:
        if base.is_dir():
            files += [f for f in base.glob("*.log") if f.is_file()]
    files.sort(key=lambda f: -f.stat().st_mtime)
    return [{"path": str(f.relative_to(ROOT)), "mtime": f.stat().st_mtime, "size": f.stat().st_size}
            for f in files[:40]]


def disk_job():
    targets = [p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")]
    targets += [p for lab in labs() for p in lab.iterdir() if p.is_dir()]
    rows = []
    for p in targets:
        try:
            out = subprocess.run(["du", "-sb", str(p)], capture_output=True, text=True, timeout=600).stdout
            rows.append({"path": str(p.relative_to(ROOT)), "bytes": int(out.split()[0])})
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            rows.append({"path": str(p.relative_to(ROOT)), "bytes": None})
    total = shutil.disk_usage(ROOT)
    DISK.update(running=False, at=time.time(), rows=rows,
                volume={"total": total.total, "used": total.used, "free": total.free})


# ---------------- 作業 ----------------

def list_tasks():
    tasks = []
    for f in sorted((HUB / "tasks").glob("*.sh")):
        t = {"id": f.stem, "title": f.stem, "desc": "", "params": []}
        for line in f.read_text(encoding="utf-8").splitlines()[:30]:
            m = re.match(r"#\s*(title|desc|param):\s*(.*)", line)
            if not m:
                continue
            key, val = m.groups()
            if key == "param":
                name, label, kind = (c.strip() for c in (val.split("|") + ["", ""])[:3])
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                    t["params"].append({"name": name, "label": label or name, "kind": kind or "text"})
            else:
                t[key] = val.strip()
        tasks.append(t)
    return tasks


def start_task(task_id, params):
    task = next((t for t in list_tasks() if t["id"] == task_id), None)
    if not task:
        raise ValueError("その作業はありません")
    env = dict(os.environ)
    for p in task["params"]:
        v = str(params.get(p["name"], ""))
        if "\0" in v or len(v) > 20000:
            raise ValueError(f"{p['label']} の値が不正です")
        env["HUB_" + p["name"]] = v
    job = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    jd = HUB / "jobs" / job
    jd.mkdir(parents=True)
    write_json(jd / "job.json", {"task": task_id, "title": task["title"], "started": time.time()})
    script = HUB / "tasks" / f"{task_id}.sh"

    def run():
        with open(jd / "out.log", "wb") as out:
            code = subprocess.call(["bash", str(script)], cwd=ROOT, env=env, stdout=out,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        (jd / "exit").write_text(str(code))

    threading.Thread(target=run, daemon=True).start()
    return job


def list_jobs():
    jobs = []
    for jd in sorted((HUB / "jobs").glob("*"), reverse=True)[:30]:
        info = read_json(jd / "job.json", {})
        code = (jd / "exit").read_text().strip() if (jd / "exit").is_file() else None
        jobs.append({"id": jd.name, "title": info.get("title", ""), "started": info.get("started"), "exit": code})
    return jobs


def job_dir(job):
    jd = HUB / "jobs" / job
    if not re.fullmatch(r"[\w-]+", job) or not jd.is_dir():
        raise FileNotFoundError(job)
    return jd


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "hub"

    def log_message(self, fmt, *args):  # アクセスのたびにログを書かない
        pass

    def authed(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "hub" in c and hmac.compare_digest(c["hub"].value, SESSION)

    def send(self, code, body, ctype="application/json; charset=utf-8", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(n, 1 << 20))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/login":
            return self.send(200, LOGIN_PAGE.replace("{{error}}", ""), "text/html; charset=utf-8")
        if not self.authed():
            if url.path.startswith("/api/") or url.path.startswith("/media/"):
                return self.send(401, {"error": "ログインしてください"})
            return self.send(302, b"", headers=[("Location", "/login")])
        try:
            if url.path == "/":
                pod = os.environ.get("RUNPOD_POD_ID", "")
                links = {"files": f"https://{pod}-8080.proxy.runpod.net" if pod else "",
                         "jupyter": f"https://{pod}-8888.proxy.runpod.net" if pod else ""}
                page = MAIN_PAGE.replace("/*LINKS*/null", json.dumps(links))
                return self.send(200, page, "text/html; charset=utf-8")
            if url.path == "/api/runs":
                return self.send(200, list_runs())
            if url.path == "/api/run":
                return self.send(200, run_detail(q.get("lab", ""), q.get("run", "")))
            if url.path == "/api/ratings":
                return self.send(200, read_json(HUB / "ratings.json", {}))
            if url.path == "/api/logs":
                return self.send(200, list_logs())
            if url.path == "/api/log":
                p = ROOT / q.get("path", "")
                if p.suffix != ".log" or not inside(p, ROOT) or not p.is_file():
                    raise FileNotFoundError(p)
                return self.send(200, {"lines": tail(p)})
            if url.path == "/api/disk":
                return self.send(200, DISK)
            if url.path == "/api/tasks":
                return self.send(200, {"tasks": list_tasks(), "jobs": list_jobs()})
            if url.path == "/api/job":
                jd = job_dir(q.get("id", ""))
                lines = tail(jd / "out.log", lines=300) if (jd / "out.log").is_file() else []
                code = (jd / "exit").read_text().strip() if (jd / "exit").is_file() else None
                return self.send(200, {"lines": lines, "exit": code})
            if url.path.startswith("/media/"):
                return self.media(urllib.parse.unquote(url.path[len("/media/"):]))
            return self.send(404, {"error": "ありません"})
        except FileNotFoundError:
            return self.send(404, {"error": "ありません"})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/login":
            form = urllib.parse.parse_qs(self.body().decode())
            if hmac.compare_digest(form.get("password", [""])[0], PASSWORD):
                cookie = f"hub={SESSION}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000"
                return self.send(302, b"", headers=[("Location", "/"), ("Set-Cookie", cookie)])
            time.sleep(1)
            page = LOGIN_PAGE.replace("{{error}}", "<p class=err>パスワードが違います</p>")
            return self.send(200, page, "text/html; charset=utf-8")
        if not self.authed():
            return self.send(401, {"error": "ログインしてください"})
        try:
            data = json.loads(self.body() or b"{}")
        except ValueError:
            return self.send(400, {"error": "不正な入力です"})
        if url.path == "/api/ratings":
            # data: {"key": "lab/run/条件[/ファイル]", "value": {...} か null}
            key = str(data.get("key", ""))
            with LOCK:
                ratings = read_json(HUB / "ratings.json", {})
                if data.get("value") is None:
                    ratings.pop(key, None)
                else:
                    ratings[key] = dict(data["value"], updated=time.time())
                write_json(HUB / "ratings.json", ratings)
            return self.send(200, {"ok": True})
        if url.path == "/api/disk":
            if not DISK["running"]:
                DISK["running"] = True
                threading.Thread(target=disk_job, daemon=True).start()
            return self.send(200, {"ok": True})
        if url.path == "/api/tasks/run":
            try:
                return self.send(200, {"job": start_task(str(data.get("task", "")), data.get("params") or {})})
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        return self.send(404, {"error": "ありません"})

    def media(self, rel):
        parts = rel.split("/", 2)
        if len(parts) != 3:
            raise FileNotFoundError(rel)
        lab, run, path = parts
        d = run_dir(lab, run)
        f = d / path
        if not inside(f, d) or not f.is_file() or f.suffix.lower() not in AUDIO | IMAGE:
            raise FileNotFoundError(rel)
        size = f.stat().st_size
        start, end = 0, size - 1
        # iPhoneのSafariは音声をRange指定で読むので、部分応答に対応する
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
        if start > end:
            return self.send(416, b"", headers=[("Content-Range", f"bytes */{size}")])
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(left, 1 << 16))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


LOGIN_PAGE = """<!doctype html><html lang=ja><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>メインページ</title>
<style>
:root{--bg:#f2f4f3;--surface:#fff;--ink:#16201c;--muted:#5a6863;--line:#d3dbd8;--accent:#0d7466;--warn:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#0f1412;--surface:#161e1b;--ink:#e2eae7;--muted:#92a19b;--line:#2a3531;--accent:#3cc2ae;--warn:#f97066}}
body{background:var(--bg);color:var(--ink);font-family:system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:24px;width:min(320px,calc(100vw - 32px));display:grid;gap:12px}
input,button{font:inherit;padding:10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
button{background:var(--accent);color:#fff;border:0;font-weight:600}.err{color:var(--warn);margin:0}
</style>
<form method=post action=/login><strong>メインページ</strong>{{error}}
<input type=password name=password placeholder=パスワード autofocus autocomplete=current-password>
<button>ログイン</button></form></html>"""

MAIN_PAGE = (Path(__file__).with_name("hub.html")).read_text(encoding="utf-8")


def main():
    HUB.mkdir(parents=True, exist_ok=True)
    (HUB / "tasks").mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[hub] {PORT}番で起動しました", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
