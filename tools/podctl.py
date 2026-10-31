#!/usr/bin/env python3
"""クラウド側（Claude Code on the web など）から RunPod の Pod を操作する。

使い方:
  python3 tools/podctl.py status                     アカウントの全Podを表示
  python3 tools/podctl.py start [--gpu 名前 ...]     Podを作り、JupyterLabが応答するまで待つ
                                                     GPUが空いていなければ、空くまで待つ（--wait-gpu 分、既定30）
  python3 tools/podctl.py start --cpu                GPUなしの安いPodを作る（AILab ハブ・ファイルの確認用）
                                                     exec / upload / download / stop も --cpu でこのPodを扱う
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
  RUNPOD_TEMPLATE_ID / RUNPOD_VOLUME_ID / RUNPOD_POD_NAME / RUNPOD_CPU_POD_NAME / RUNPOD_GPU_TYPES・RUNPOD_CPU_FLAVORS（カンマ区切り）

通信はすべて JupyterLab（8888番）へのHTTPS。クラウド環境からは SSH も WebSocket も使えないため、
exec はコマンドを /workspace/logs/podctl/<Pod ID>/<id>/cmd.sh に置き、Pod内の scripts/podctl-runner.sh が
実行して出力を同じフォルダに書く。それを読み取って表示する。
"""
import argparse
import base64
import json
import os
import re
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
# CPUのPodは別の名前にして、GPUのPodと同時に動かせるようにする
CPU_POD_NAME = os.environ.get("RUNPOD_CPU_POD_NAME", "Hub-cpu-auto")
# 先頭から順に空きのあるものが使われる。費用と環境の互換性から、普段は RTX 4090 だけに限る
# （空いていなければ別のGPUにせず待つ。ユーザーと決めたルール）
GPU_TYPES = os.environ.get("RUNPOD_GPU_TYPES", "NVIDIA GeForce RTX 4090").split(",")
CPU_VCPUS = 2
# CPUの種類（先頭から順に空きのあるものが使われる）。cpu3c は特定のマシンで外からつながらない不具合があったので
# 後回しにし、ほぼ同じ料金（2 vCPUで$0.06〜0.07/時）の別の種類を先に試す
CPU_FLAVORS = os.environ.get("RUNPOD_CPU_FLAVORS", "cpu5c,cpu3g,cpu5g,cpu3c").split(",")
# CPUのPodで開くポート（AILab ハブ、JupyterLab、追加のWebアプリ用の3000番）。
# ComfyUIは動かさない。3000番で何を動かすかはVolumeの /workspace/startup.d 側で決める
CPU_PORTS = ["8000/http", "8888/http", "3000/http"]
JUPYTER_PORT = 8888


def die(msg):
    print(f"[podctl] {msg}", file=sys.stderr)
    sys.exit(1)


def auth_header(env, prefix):
    """環境変数があればヘッダーを作る。無ければ空にし、クラウド環境の
    Network secrets（通信時にプロキシがヘッダーを付ける）に任せる。"""
    value = os.environ.get(env)
    return {"Authorization": f"{prefix} {value}"} if value else {}


class Unavailable(Exception):
    """指定したGPU（またはCPU）に空きが無い"""


def runpod(method, path, body=None, allow_404=False):
    r = requests.request(method, API_BASE + path, json=body, timeout=60,
                         headers=auth_header("RUNPOD_API_KEY", "Bearer"))
    if allow_404 and r.status_code == 404:
        return None
    if method == "POST" and "no instances currently available" in r.text:
        raise Unavailable(r.text[:200])
    if not r.ok:
        hint = ""
        if method == "POST":
            hint = "（エラーでも作成されている場合がある。status で確認してから再実行する）"
        die(f"RunPod API エラー {r.status_code}: {r.text[:500]}{hint}")
    return r.json() if r.text else None


def pod_name(args):
    return CPU_POD_NAME if args.cpu else POD_NAME


def managed_pods(name):
    return [p for p in (runpod("GET", "/pods") or []) if p.get("name") == name]


def running_pod(args):
    pods = [p for p in managed_pods(pod_name(args)) if p.get("desiredStatus") == "RUNNING"]
    if not pods:
        die(f"実行中のPod '{pod_name(args)}' がありません。start{' --cpu' if args.cpu else ''} で作成してください")
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
        if not p.get("gpuCount"):
            gpu = f"CPU {p.get('vcpuCount')} vCPU"
        mark = " ← このツールの管理対象" if p.get("name") in (POD_NAME, CPU_POD_NAME) else ""
        print(f"{p['id']}  {p.get('name')}  {p.get('desiredStatus')}  ${p.get('costPerHr')}/時  {gpu}{mark}")


def cmd_start(args):
    name = pod_name(args)
    existing = managed_pods(name)
    if existing:
        p = existing[0]
        print(f"Pod '{name}' は既にあります: {p['id']}（{p.get('desiredStatus')}）。新しくは作りません")
    else:
        body = {
            "name": name,
            "templateId": TEMPLATE_ID,
            "networkVolumeId": VOLUME_ID,
        }
        if args.cpu:
            # 2 vCPU・メモリ4GB。ファイルの確認や取り出し、軽いコマンド向け
            body.update({"computeType": "CPU", "vcpuCount": CPU_VCPUS, "ports": CPU_PORTS, "cpuFlavorIds": CPU_FLAVORS})
        else:
            body.update({"gpuTypeIds": args.gpu or GPU_TYPES, "gpuCount": 1})
        # 決めたGPUだけを使い、空いていなければ別のGPUにせず、空くまで待つ
        give_up = time.time() + args.wait_gpu * 60
        while True:
            try:
                p = runpod("POST", "/pods", body)
                break
            except Unavailable:
                if time.time() > give_up:
                    die(f"{args.wait_gpu}分待っても空きがありませんでした（{'CPU' if args.cpu else '・'.join(body['gpuTypeIds'])}）。"
                        "時間をおいて start し直してください")
                print("空きがありません。1分後にもう一度試します", flush=True)
                time.sleep(60)
        if not p or "id" not in p:
            die("応答にPod IDがありません。status で確認してください")
        print(f"Podを作成しました: {p['id']}（${p.get('costPerHr')}/時）")

    deadline = time.time() + args.wait * 60
    while time.time() < deadline:
        try:
            r = jupyter(p, "GET", "/api/status", timeout=15)
            if r.status_code == 200:
                print(f"準備完了: {jupyter_base(p)}")
                if args.cpu:
                    print(f"AILab ハブ: https://{p['id']}-8000.proxy.runpod.net")
                    print(f"3000番: https://{p['id']}-3000.proxy.runpod.net")
                return
            if r.status_code == 403:
                die("JupyterLabにログインできません。テンプレートの JUPYTER_PASSWORD と、こちらの設定が同じ値か確認してください")
        except requests.RequestException:
            pass
        time.sleep(10)
    die(f"{args.wait}分待っても応答がありません。Podは動いている可能性があるので、status を確認し、不要なら stop してください")


JOBS_DIR = "logs/podctl"  # /workspace からの相対パス。Pod側は scripts/podctl-runner.sh が見る


def _contents_path(remote):
    remote = remote.removeprefix("/workspace/").lstrip("/")
    return "/api/contents/" + urllib.parse.quote(remote)


def _ensure_dir(pod, remote):
    """/workspace からの相対パスのフォルダを、上から順に作る（あれば何もしない）"""
    path = ""
    for part in remote.strip("/").split("/"):
        path = f"{path}/{part}" if path else part
        if jupyter(pod, "GET", _contents_path(path) + "?content=0").ok:
            continue
        # 起動直後は404を返すことがあるので、少し待って試し直す
        for _ in range(3):
            r = jupyter(pod, "PUT", _contents_path(path), json={"type": "directory"})
            if r.ok:
                break
            time.sleep(5)
        if not r.ok:
            die(f"フォルダを作れません: /workspace/{path}: {r.status_code} {r.text[:300]}")


def _job_files(pod, job):
    r = jupyter(pod, "GET", _contents_path(f"{JOBS_DIR}/{job}"), timeout=30)
    return {c["name"] for c in r.json().get("content", [])} if r.ok else set()


def _read_file(pod, remote):
    # 中継でキャッシュされないよう、毎回違うクエリを付ける
    r = jupyter(pod, "GET", "/files/" + urllib.parse.quote(remote) + f"?t={time.time()}", timeout=60)
    return r.content if r.ok else None


def cmd_exec(args):
    # クラウド環境の通信はWebSocket（JupyterLabのターミナル）を通さないので、
    # コマンドをファイルで置き、Pod内のランナーに実行させて結果のファイルを読む
    pod = running_pod(args)
    # ランナーはPodごとのフォルダ（Pod ID）だけを見る。/workspace は全Podで共有なので、
    # 分けないと同時に動いている別のPodがコマンドを拾ってしまう
    job = f"{pod['id']}/" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    jobdir = f"{JOBS_DIR}/{job}"
    _ensure_dir(pod, jobdir)
    r = jupyter(pod, "PUT", _contents_path(f"{jobdir}/cmd.sh"),
                json={"type": "file", "format": "text", "content": args.command + "\n"})
    if not r.ok:
        die(f"コマンドを送れません: {r.status_code} {r.text[:300]}")

    shown = 0
    started_by = time.time() + 30
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(1)
        files = _job_files(pod, job)
        if "started" not in files:
            if time.time() > started_by:
                die("Pod内のランナーが動いていません。テンプレートのイメージが古い可能性があります"
                    "（scripts/podctl-runner.sh を起動するイメージで作り直す）")
            continue
        done = "exit" in files
        out = _read_file(pod, f"{jobdir}/out.log") if "out.log" in files else None
        if out and len(out) > shown:
            sys.stdout.write(out[shown:].decode("utf-8", "replace"))
            sys.stdout.flush()
            shown = len(out)
        if done:
            code = (_read_file(pod, f"{jobdir}/exit") or b"").strip()
            sys.exit(int(code) if code.isdigit() else 1)
    print(f"\n[podctl] {args.timeout}秒で打ち切りました。コマンドはPod内で動き続けています"
          f"（出力: /workspace/{jobdir}/out.log。長い処理は nohup で起動し、ログを見る）", file=sys.stderr)
    sys.exit(124)


def cmd_upload(args):
    pod = running_pod(args)
    with open(args.local, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    body = {"type": "file", "format": "base64", "content": data}
    parent = os.path.dirname(args.remote.removeprefix("/workspace/").lstrip("/"))
    if parent:
        _ensure_dir(pod, parent)
    r = jupyter(pod, "PUT", _contents_path(args.remote), json=body, timeout=300)
    if not r.ok:
        die(f"送信に失敗しました: {r.status_code} {r.text[:300]}")
    print(f"送信しました: /workspace/{r.json()['path']}")


def cmd_download(args):
    pod = running_pod(args)
    remote = args.remote.removeprefix("/workspace/").lstrip("/")
    r = jupyter(pod, "GET", "/files/" + urllib.parse.quote(remote), stream=True, timeout=300)
    if not r.ok:
        die(f"受信に失敗しました: {r.status_code}")
    with open(args.local, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    print(f"受信しました: {args.local}（{os.path.getsize(args.local)} バイト）")


def cmd_stop(args):
    pods = managed_pods(pod_name(args))
    if not pods:
        print(f"Pod '{pod_name(args)}' はありません。削除するものはありません")
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
    g = s.add_mutually_exclusive_group()
    g.add_argument("--gpu", nargs="+", help="GPUの種類（先頭から順に試す）")
    g.add_argument("--cpu", action="store_true", help="GPUなしの安いPodにする（AILab ハブ・ファイルの確認用）")
    s.add_argument("--wait", type=int, default=10, help="JupyterLabの応答を待つ分数")
    s.add_argument("--wait-gpu", type=int, default=30, help="空きが無いとき、空くまで待つ分数（0なら待たない）")
    e = sub.add_parser("exec")
    e.add_argument("--cpu", action="store_true", help="CPUのPodを扱う")
    e.add_argument("command")
    e.add_argument("--timeout", type=int, default=600)
    u = sub.add_parser("upload")
    u.add_argument("--cpu", action="store_true", help="CPUのPodを扱う")
    u.add_argument("local")
    u.add_argument("remote")
    d = sub.add_parser("download")
    d.add_argument("--cpu", action="store_true", help="CPUのPodを扱う")
    d.add_argument("remote")
    d.add_argument("local")
    t = sub.add_parser("stop")
    t.add_argument("--cpu", action="store_true", help="CPUのPodを扱う")
    t.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"status": cmd_status, "start": cmd_start, "exec": cmd_exec, "upload": cmd_upload,
     "download": cmd_download, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    main()
