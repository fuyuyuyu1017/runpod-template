#!/usr/bin/env python3
"""Pod内で動くメインページ（8000番）。中身はPodの中だけで扱い、外のサービスには送らない。

- 結果：/workspace/*-lab/generated/<実行>/ の画像・音声を一覧にし、見る・聞く・評価する
  （評価は /workspace/hub/ratings.json に保存。実行フォルダに hub.json があれば、表示名・説明と
    条件の並び順・表示名・評価項目に使う。評価項目は <分野>-lab/setup/hub.json で分野ごとの既定も置ける）
  ほかに探す場所（ComfyUIの出力など）は /workspace/hub/sources.json で足せる。PNGの生成情報も読む
- 確認：ログの末尾、容量の内訳
- 作業：/workspace/hub/tasks/*.sh を、決めた入力欄だけでボタンから実行する
  （スクリプトの先頭に "# title: 名前" "# desc: 説明" "# param: 変数名 | 表示名 | text か textarea"。
    入力値は環境変数 HUB_<変数名> で渡す）

- Pod：CPUのPodでは、3000番のアプリへのリンクと「Podを削除」ボタンを出す
  （削除はRunPodがPodに渡す RUNPOD_API_KEY で自分自身だけを消す）

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
import struct
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
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


ROOT_RUN = "（直下）"  # 足した探す場所の直下にあるファイル（ComfyUIの output/ など）をまとめた実行の名前


def sources():
    """結果を探す場所 {名前: (フォルダ, 分野か)}。分野は <分野>-lab/generated/<実行>/。
    足す場所は /workspace/hub/sources.json（{"名前": "/workspace からの相対パス"}）。無ければ ComfyUI の出力"""
    out = {lab.name: (lab / "generated", True) for lab in labs()}
    extra = read_json(HUB / "sources.json", None)
    if not isinstance(extra, dict):
        extra = {"ComfyUI": "apps/ComfyUI/output"}
    for name, rel in extra.items():
        d = ROOT / str(rel)
        if re.fullmatch(r"[\w.-]+", name) and name not in out and inside(d, ROOT) and d.is_dir():
            out[name] = (d, False)
    return out


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

def run_meta(d):
    """実行フォルダの hub.json（任意）。例：
    {"title": "LoRA ステップ比較", "note": "同じ15文",
     "conditions": {"s1000": {"label": "1000ステップ"}, "final": {"label": "3000ステップ", "note": "最後"}}}
    conditions に書いた順が並び順になる。書いていない条件は後ろに名前順（数字は数として）で並ぶ。"""
    meta = read_json(d / "hub.json", {})
    return meta if isinstance(meta, dict) else {}


def natural_key(name):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", name) if t]


def media_files(d, recursive=True):
    for f in sorted(d.rglob("*") if recursive else d.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO | IMAGE:
            yield f


def list_runs():
    runs = []
    for name, (base, is_lab) in sources().items():
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if d.is_dir():
                meta = run_meta(d)
                runs.append({"lab": name, "run": d.name, "mtime": d.stat().st_mtime,
                             "title": str(meta.get("title", "")), "note": str(meta.get("note", ""))})
        if not is_lab and next(media_files(base, recursive=False), None):
            runs.append({"lab": name, "run": ROOT_RUN, "mtime": base.stat().st_mtime, "title": f"{name}{ROOT_RUN}", "note": ""})
    return sorted(runs, key=lambda r: -r["mtime"])


def run_dir(lab, run):
    src = sources().get(lab)
    if not src:
        raise FileNotFoundError(lab)
    base, is_lab = src
    if run == ROOT_RUN and not is_lab:
        return base
    d = base / run
    if "/" in run or run in ("", ".", "..") or not inside(d, base) or not d.is_dir():
        raise FileNotFoundError(run)
    return d


def fields(items):
    """評価項目：["自然さ", ...] か [{"key": "natural", "label": "自然さ"}, ...]"""
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("key"):
            out.append({"key": str(it["key"]), "label": str(it.get("label") or it["key"])})
        elif isinstance(it, str) and it:
            out.append({"key": it, "label": it})
    return out


def sentences_for(d, is_lab=True):
    """実行フォルダか、その上の setup/ にある sentences.tsv（タグ・指示・本文）を読む"""
    for p in [d / "sentences.tsv"] + ([d.parent.parent / "setup" / "compare_sentences.tsv"] if is_lab else []):
        if p.is_file():
            # 音声の名前は「番号_タグ」。同じタグの文が何本もあることがあるので、番号（#1から）でも引けるようにする
            rows, n = {}, 0
            for line in p.read_text(encoding="utf-8").splitlines():
                cols = line.split("\t")
                if line.startswith("#") or len(cols) < 3:
                    continue
                n += 1
                row = {"caption": cols[1], "text": cols[2]}
                rows["#%d" % n] = row
                rows.setdefault(cols[0], row)
            return rows
    return {}


def run_detail(lab, run):
    d = run_dir(lab, run)
    is_lab = sources()[lab][1]
    conds = {}
    for f in media_files(d, recursive=run != ROOT_RUN):
        rel = f.relative_to(d)
        cond = str(rel.parent) if str(rel.parent) != "." else "（直下）"
        conds.setdefault(cond, []).append({
            "name": f.name, "path": str(rel), "kind": "audio" if f.suffix.lower() in AUDIO else "image",
            "mtime": f.stat().st_mtime,
        })
    meta = run_meta(d)
    # 評価項目：実行の hub.json → 分野の setup/hub.json の順。どちらにも無ければ ☆ とメモだけ
    lab_meta = read_json(ROOT / lab / "setup" / "hub.json", {}) if is_lab else {}
    lab_meta = lab_meta if isinstance(lab_meta, dict) else {}
    scores = fields(meta.get("scores", lab_meta.get("scores")))
    texts = fields(meta.get("texts", lab_meta.get("texts")))
    info = meta.get("conditions") if isinstance(meta.get("conditions"), dict) else {}
    order = {name: i for i, name in enumerate(info)}
    names = sorted(conds, key=lambda c: (order.get(c, len(order)), natural_key(c)))
    # 条件名が数字だけだとJSのオブジェクトでは並びが変わるので、配列で返す
    conditions = []
    for c in names:
        ci = info.get(c) if isinstance(info.get(c), dict) else {}
        conditions.append({"name": c, "label": str(ci.get("label") or c), "note": str(ci.get("note", "")),
                           "files": conds[c]})
    title = str(meta.get("title", "")) or (f"{lab}{ROOT_RUN}" if run == ROOT_RUN else "")
    return {"lab": lab, "run": run, "title": title, "note": str(meta.get("note", "")),
            "conditions": conditions, "sentences": sentences_for(d, is_lab), "scores": scores, "texts": texts}


# PNGに埋め込まれた生成情報（ComfyUI は prompt・workflow、ほかのツールは parameters など）

def png_text(path, limit=4 << 20):
    out = {}
    with open(path, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            return out
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            size, kind = struct.unpack(">I4s", head)
            if kind in (b"tEXt", b"zTXt", b"iTXt") and size <= limit:
                data = f.read(size)
                key, _, rest = data.partition(b"\0")
                try:
                    if kind == b"zTXt":
                        rest = zlib.decompress(rest[1:])
                    elif kind == b"iTXt":
                        flag, rest = rest[0], rest[2:]
                        rest = rest.split(b"\0", 2)[2]
                        if flag:
                            rest = zlib.decompress(rest)
                    out[key.decode("latin-1")] = rest.decode("utf-8", "replace")
                except (zlib.error, IndexError):
                    pass
                f.seek(4, 1)
            elif kind == b"IEND":
                break
            else:
                f.seek(size + 4, 1)
    return out


def comfy_summary(prompt):
    """ComfyUI の prompt（ノードの一覧）から、見比べたい値だけ抜き出す"""
    try:
        nodes = json.loads(prompt)
    except ValueError:
        return {}
    out = {"プロンプト": [], "モデル": [], "LoRA": [], "設定": []}
    for n in nodes.values() if isinstance(nodes, dict) else []:
        cls, inp = str(n.get("class_type", "")), n.get("inputs") or {}
        if isinstance(inp.get("text"), str) and "TextEncode" in cls:
            out["プロンプト"].append(inp["text"])
        if isinstance(inp.get("ckpt_name"), str):
            out["モデル"].append(inp["ckpt_name"])
        if isinstance(inp.get("lora_name"), str):
            out["LoRA"].append(f'{inp["lora_name"]}（{inp.get("strength_model", "")}）')
        if "seed" in inp or "noise_seed" in inp:
            vals = [f"{k}={inp[k]}" for k in ("seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler", "denoise")
                    if k in inp and not isinstance(inp[k], list)]
            out["設定"].append(f"{cls}: " + " ".join(vals))
    return {k: v for k, v in out.items() if v}


def file_meta(lab, run, path):
    d = run_dir(lab, run)
    f = d / path
    if not inside(f, d) or not f.is_file():
        raise FileNotFoundError(path)
    texts = png_text(f) if f.suffix.lower() == ".png" else {}
    summary = comfy_summary(texts["prompt"]) if "prompt" in texts else {}
    if "parameters" in texts:
        summary.setdefault("生成情報", [texts["parameters"]])
    st = f.stat()
    return {"summary": summary, "raw": {k: v[:20000] for k, v in texts.items()},
            "size": st.st_size, "mtime": st.st_mtime}


# ---------------- Pod ----------------

def pod_info():
    """RunPodのPodの中で動いているときだけ中身がある。PCでは空"""
    pod = os.environ.get("RUNPOD_POD_ID", "")
    if not pod:
        return {}
    info = {"id": pod,
            "files": f"https://{pod}-8080.proxy.runpod.net",
            "jupyter": f"https://{pod}-8888.proxy.runpod.net"}
    try:
        with socket.create_connection(("127.0.0.1", 3000), timeout=0.3):
            info["app3000"] = f"https://{pod}-3000.proxy.runpod.net"
    except OSError:
        pass
    # 削除ボタンはCPUのPodだけ（GPUのPodは学習中かもしれないので、ここからは消さない）
    if os.environ.get("RUNPOD_API_KEY") and os.environ.get("RUNPOD_GPU_COUNT", "0") in ("", "0"):
        info["deletable"] = True
    return info


def delete_self():
    pod = os.environ["RUNPOD_POD_ID"]
    body = json.dumps({"query": "mutation($id: String!) { podTerminate(input: {podId: $id}) }",
                       "variables": {"id": pod}}).encode()
    req = urllib.request.Request("https://api.runpod.io/graphql", data=body, headers={
        "Content-Type": "application/json", "User-Agent": "runpod-template-hub",
        "Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read() or b"{}")
    if res.get("errors"):
        raise RuntimeError(res["errors"][0].get("message", "削除できませんでした"))


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
                page = MAIN_PAGE.replace("/*POD*/null", json.dumps(pod_info()))
                return self.send(200, page, "text/html; charset=utf-8")
            if url.path == "/api/runs":
                return self.send(200, list_runs())
            if url.path == "/api/run":
                return self.send(200, run_detail(q.get("lab", ""), q.get("run", "")))
            if url.path == "/api/meta":
                return self.send(200, file_meta(q.get("lab", ""), q.get("run", ""), q.get("path", "")))
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
        if url.path == "/api/pod/delete":
            info = pod_info()
            if not info.get("deletable") or data.get("confirm") != info["id"]:
                return self.send(400, {"error": "このPodはここから削除できません"})
            try:
                delete_self()
            except Exception as e:  # noqa: BLE001  画面に理由を出す
                print(f"[hub] Podの削除に失敗: {e}", flush=True)
                return self.send(502, {"error": f"削除できませんでした（{e}）"})
            print("[hub] Podの削除を頼みました", flush=True)
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
