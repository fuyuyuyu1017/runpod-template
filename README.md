# runpod-template

RunPod用の自作テンプレートです。イメージは軽くして、ComfyUI本体・仮想環境・モデルはNetwork Volume（`/workspace`）に置きます。

## イメージに入っているもの

- CUDA 12.8（ベースのみ。PyTorchなどは各仮想環境が持つ）
- Python 3.12（`/usr/bin/python3.12`）
- git、ffmpeg、sox、ビルドツール、SSH
- JupyterLab（8888番）
- ファイルブラウザ（8080番）
- 起動スクリプト `start.sh`

`main` ブランチに変更が入ると、GitHub Actionsがイメージをビルドして `ghcr.io/fuyuyuyu1017/runpod-template:latest` に置きます。

## 起動時の動き（start.sh）

1. RunPodに公開鍵が設定されていればSSHを起動
2. JupyterLabを起動（パスワードは環境変数 `JUPYTER_PASSWORD`。未設定ならランダムに作ってログに出す）
3. ファイルブラウザを起動（初回だけユーザー `admin` を作る。パスワードは `FILEBROWSER_PASSWORD`、未設定ならランダムに作ってログに出す）
4. `/workspace/apps/ComfyUI` があればComfyUIを8188番で起動
5. `/workspace/startup.d/*.sh` があれば順に実行

ログは `/workspace/logs/` に保存されます。

## RunPodのテンプレート設定

| 項目 | 値 |
| --- | --- |
| Container Image | `ghcr.io/fuyuyuyu1017/runpod-template:latest` |
| Container Disk | 20 GB |
| Volume Mount Path | `/workspace` |
| HTTP Ports | `8188, 8888, 8080` |
| TCP Ports | `22` |
| Environment Variables | `JUPYTER_PASSWORD`、`FILEBROWSER_PASSWORD`（任意。ファイルブラウザは12文字以上が必須） |

## ComfyUIのインストール（初回だけ）

```bash
bash /opt/runpod-template/scripts/install-comfyui.sh
```

- `/workspace/apps/ComfyUI` に最新のリリース版を入れる（`COMFY_REF=v0.x.y` で版を指定できる）
- 既存の `/workspace/runpod-slim/ComfyUI/models` を `extra_model_paths.yaml` で参照する（モデルはコピーしない）
- 再実行すると最新のリリースへ更新する

## クラウドのClaudeから操作する（tools/podctl.py）

Claude Code on the web などのクラウド環境から、Podの作成・コマンド実行・ファイル転送・削除をするツールです。クラウド環境からはSSHが使えないため、コマンドはJupyterLab（8888番）のターミナルをHTTPSで使って実行します。

```bash
pip install websocket-client          # 初回だけ
python3 tools/podctl.py status
python3 tools/podctl.py start          # 既にあれば作らない。JupyterLabが応答するまで待つ
python3 tools/podctl.py exec 'nvidia-smi'
python3 tools/podctl.py upload ./train.sh voice-lab/setup/train.sh
python3 tools/podctl.py download voice-lab/generated/out.wav ./out.wav
python3 tools/podctl.py stop --yes     # Podを削除（/workspace は残る）
```

クラウド環境の設定：

| 項目 | 値 |
| --- | --- |
| Allowed domains | `rest.runpod.io`、`*.proxy.runpod.net` |
| Secrets / 環境変数 | `RUNPOD_API_KEY`（この用途専用のキー）、`JUPYTER_PASSWORD`（RunPodのテンプレートと同じ値） |

- Pod名は `runpod.ps1` と同じ（`ComfyUI-session-auto`）なので、PCとクラウドのどちらからでも同じPodを扱えます。
- `exec` は既定で600秒で打ち切ります。学習など長い処理は `nohup ... > /workspace/logs/xxx.log 2>&1 &` で起動し、ログを見ます。
- GPUは `--gpu "NVIDIA RTX A5000" "NVIDIA GeForce RTX 3090"` のように複数指定でき、空きのあるものが使われます。
