#!/usr/bin/env python3
"""クラウド側（Claude Code on the web など）から RunPod の Pod を操作する。

使い方:
  python3 tools/podctl.py status                     アカウントの全Podを表示
  python3 tools/podctl.py start [--gpu 名前 ...]     Podを作り、JupyterLabが応答するまで待つ
  python3 tools/podctl.py exec 'コマンド' [--timeout 秒]
                                                     Pod内のbashでコマンドを実行し、出力と終了コードを返す
  python3 tools/podctl.py upload ローカル リモート   ファイルを送る（リモートは /workspace からの相対パス）
  python3 tools/podctl.py download リモート ローカル ファイルを受け取る
  python3 tools/podctl.py stop --yes                 このツールが管理するPodを削除（/workspace は残る）

認証（どちらか）:
  - クラウド環境の Network secrets（おすすめ。キーはセッションから見えない）
      rest.runpod.io       → Authorization: Bearer <RunPodのAPIキー>
      *.proxy.runpod.net   → Authorization: token <JupyterLabのパスワード>
  - 環境変数 RUNPOD_API_KEY / JUPYTER_PASSWORD
  JupyterLabのパスワードは、RunPodのテンプレートの JUPYTER_PASSWORD と同じ値にする
任意:
  RUNPOD_TEMPLATE_ID / RUNPOD_VOLUME_ID / RUNPOD_POD_NAME / RUNPOD_GPU_TYPES（カンマ区切り）

コマンドの実行には JupyterLab のターミナル（8888番、HTTPS）を使う。
クラウド環境からは SSH が使えないため。
"""
import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import uuid

import requests

API_BASE = "https://rest.runpod.io/v1"
TEMPLATE_ID = os.environ.get("RUNPOD_TEMPLATE_ID", "bxcjnehhh6")
VOLUME_ID = os.environ.get("RUNPOD_VOLUME_ID", "cnkmw89wfz")
# runpod.ps1 と同じ名前にして、どちらからでも同じPodを扱えるようにする
POD_NAME = os.environ.get("RUNPOD_POD_NAME", "ComfyUI-session-auto")
# 先頭から順に空きのあるものが使われる
GPU_TYPES = os.environ.get("RUNPOD_GPU_TYPES", "NVIDIA GeForce RTX 4090").split(",")
JUPYTER_PORT = 8888


def die(msg):
    print(f"[podctl] {msg}", file=sys.stderr)
    sys.exit(1)


def auth_header(env, prefix):
    """環境変数があればヘッダーを作る。無ければ空にし、クラウド環境の
    Network secrets（通信時にプロキシがヘッダーを付ける）に任せる。"""
    value = os.environ.get(env)
    return {"Authorization": f"{prefix} {value}"} if value else {}


def runpod(method, path, body=None, allow_404=False):
    r = requests.request(method, API_BASE + path, json=body, timeout=60,
                         headers=auth_header("RUNPOD_API_KEY", "Bearer"))
    if allow_404 and r.status_code == 404:
        return None
    if not r.ok:
        hint = ""
        if method == "POST":
            hint = "（エラーでも作成されている場合がある。status で確認してから再実行する）"
        die(f"RunPod API エラー {r.status_code}: {r.text[:500]}{hint}")
    return r.json() if r.text else None


def managed_pods():
    return [p for p in (runpod("GET", "/pods") or []) if p.get("name") == POD_NAME]


def running_pod():
    pods = [p for p in managed_pods() if p.get("desiredStatus") == "RUNNING"]
    if not pods:
        die(f"実行中のPod '{POD_NAME}' がありません。start で作成してください")
    return pods[0]


def jupyter_base(pod):
    return f"https://{pod['id']}-{JUPYTER_PORT}.proxy.runpod.net"


def jupyter(pod, method, path, **kw):
    headers = auth_header("JUPYTER_PASSWORD", "token")
    return requests.request(method, jupyter_base(pod) + path, headers=headers,
                            timeout=kw.pop("timeout", 60), **kw)


# ---------------- commands ----------------

def cmd_status(_args):
    pods = runpod("GET", "/pods") or []
    if not pods:
        print("Podはありません（GPUの課金は発生していません）")
        return
    for p in pods:
        gpu = (p.get("machine") or {}).get("gpuTypeId") or (p.get("gpu") or {}).get("id", "")
        mark = " ← このツールの管理対象" if p.get("name") == POD_NAME else ""
        print(f"{p['id']}  {p.get('name')}  {p.get('desiredStatus')}  ${p.get('costPerHr')}/時  {gpu}{mark}")


def cmd_start(args):
    existing = managed_pods()
    if existing:
        p = existing[0]
        print(f"Pod '{POD_NAME}' は既にあります: {p['id']}（{p.get('desiredStatus')}）。新しくは作りません")
    else:
        body = {
            "name": POD_NAME,
            "templateId": TEMPLATE_ID,
            "networkVolumeId": VOLUME_ID,
            "gpuTypeIds": args.gpu or GPU_TYPES,
            "gpuCount": 1,
        }
        p = runpod("POST", "/pods", body)
        if not p or "id" not in p:
            die("応答にPod IDがありません。status で確認してください")
        print(f"Podを作成しました: {p['id']}（${p.get('costPerHr')}/時）")

    deadline = time.time() + args.wait * 60
    while time.time() < deadline:
        try:
            r = jupyter(p, "GET", "/api/status", timeout=15)
            if r.status_code == 200:
                print(f"準備完了: {jupyter_base(p)}")
                return
            if r.status_code == 403:
                die("JupyterLabにログインできません。テンプレートの JUPYTER_PASSWORD と、こちらの設定が同じ値か確認してください")
        except requests.RequestException:
            pass
        time.sleep(10)
    die(f"{args.wait}分待っても応答がありません。Podは動いている可能性があるので、status を確認し、不要なら stop してください")


def _ws_connect(pod, term_name):
    import websocket  # websocket-client（無ければ pip install websocket-client）

    url = re.sub(r"^http", "ws", jupyter_base(pod)) + f"/terminals/websocket/{term_name}"
    opts = {"header": [f"{k}: {v}" for k, v in auth_header("JUPYTER_PASSWORD", "token").items()],
            "sslopt": {"cert_reqs": ssl.CERT_REQUIRED,
                       "ca_certs": os.environ.get("SSL_CERT_FILE") or None},
            "timeout": 30}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        u = urllib.parse.urlparse(proxy)
        opts.update(http_proxy_host=u.hostname, http_proxy_port=u.port, proxy_type="http")
    return websocket.create_connection(url, **opts)


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def cmd_exec(args):
    import websocket

    pod = running_pod()
    r = jupyter(pod, "POST", "/api/terminals")
    if not r.ok:
        die(f"ターミナルを開けません: {r.status_code} {r.text[:300]}")
    name = r.json()["name"]
    tag = uuid.uuid4().hex[:8]
    # 出力の始まりと終わりの目印。入力の表示（エコー）やプロンプトと区別できるよう、実行時に組み立てる
    start = f"__PODCTL_{tag}_START__"
    done = re.compile(rf"__PODCTL_{tag}_END_(\d+)__\n?")
    # ( ) で囲み、exit や cd が後ろの目印の出力に影響しないようにする。
    # 終わりの目印は ) と同じ行に置き、bashが実行前にまとめて読むようにする（実行中の入力のエコーを防ぐ）
    script = (f"printf '__PODCTL_%s_START__\\n' {tag}; (cd /workspace\n{args.command}\n); "
              f"printf '__PODCTL_%s_END_%s__\\n' {tag} $?\n")
    ws = _ws_connect(pod, name)
    ws.settimeout(5)
    code = None
    try:
        ws.send(json.dumps(["stdin", script]))
        buf = ""
        started = False
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg[0] != "stdout":
                continue
            buf += ANSI.sub("", msg[1])
            if not started:
                i = buf.find(start + "\n")
                if i < 0:
                    continue
                buf = buf[i + len(start) + 1:]
                started = True
            m = done.search(buf)
            if m:
                sys.stdout.write(buf[:m.start()])
                code = int(m.group(1))
                break
            # 目印が途中で切れている可能性があるので、末尾は残して出力する
            keep = 64
            if len(buf) > keep:
                sys.stdout.write(buf[:-keep])
                buf = buf[-keep:]
            sys.stdout.flush()
        else:
            if started:
                sys.stdout.write(buf)
            print(f"\n[podctl] {args.timeout}秒で打ち切りました。コマンドはPod内で動き続けている可能性があります"
                  "（長い処理は nohup で起動し、ログを見る）", file=sys.stderr)
    finally:
        ws.close()
        jupyter(pod, "DELETE", f"/api/terminals/{name}")
    sys.exit(124 if code is None else code)


def _contents_path(remote):
    remote = remote.removeprefix("/workspace/").lstrip("/")
    return "/api/contents/" + urllib.parse.quote(remote)


def cmd_upload(args):
    pod = running_pod()
    with open(args.local, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    body = {"type": "file", "format": "base64", "content": data}
    r = jupyter(pod, "PUT", _contents_path(args.remote), json=body, timeout=300)
    if not r.ok:
        die(f"送信に失敗しました: {r.status_code} {r.text[:300]}（送り先のフォルダが無い場合は exec 'mkdir -p ...' で作る）")
    print(f"送信しました: /workspace/{r.json()['path']}")


def cmd_download(args):
    pod = running_pod()
    remote = args.remote.removeprefix("/workspace/").lstrip("/")
    r = jupyter(pod, "GET", "/files/" + urllib.parse.quote(remote), stream=True, timeout=300)
    if not r.ok:
        die(f"受信に失敗しました: {r.status_code}")
    with open(args.local, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    print(f"受信しました: {args.local}（{os.path.getsize(args.local)} バイト）")


def cmd_stop(args):
    pods = managed_pods()
    if not pods:
        print(f"Pod '{POD_NAME}' はありません。削除するものはありません")
        return
    if not args.yes:
        for p in pods:
            print(f"削除対象: {p['id']}（{p.get('desiredStatus')}）")
        die("/workspace 以外のデータは消えます。削除するには --yes を付けて再実行してください")
    for p in pods:
        runpod("DELETE", f"/pods/{p['id']}", allow_404=True)
        print(f"削除しました（または既に無い）: {p['id']}")


def main():
    ap = argparse.ArgumentParser(description="RunPod の Pod をクラウド側から操作する")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("start")
    s.add_argument("--gpu", nargs="+", help="GPUの種類（先頭から順に試す）")
    s.add_argument("--wait", type=int, default=10, help="JupyterLabの応答を待つ分数")
    e = sub.add_parser("exec")
    e.add_argument("command")
    e.add_argument("--timeout", type=int, default=600)
    u = sub.add_parser("upload")
    u.add_argument("local")
    u.add_argument("remote")
    d = sub.add_parser("download")
    d.add_argument("remote")
    d.add_argument("local")
    t = sub.add_parser("stop")
    t.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"status": cmd_status, "start": cmd_start, "exec": cmd_exec, "upload": cmd_upload,
     "download": cmd_download, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    main()
