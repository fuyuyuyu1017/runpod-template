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
4. `/workspace/apps/ComfyUI` があり、GPUがあればComfyUIを8188番で起動（CPUのPodでは起動しない）
5. メインページを8000番で起動（下の「メインページ」）
6. `/workspace/startup.d/*.sh` があれば順に実行

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

Claude Code on the web などのクラウド環境から、Podの作成・コマンド実行・ファイル転送・削除をするツールです。クラウド環境からはSSHもWebSocket（JupyterLabのターミナル）も使えないため、通信はすべてJupyterLab（8888番）へのHTTPSで行います。`exec` はコマンドを `/workspace/logs/podctl/<id>/cmd.sh` に置き、Pod内のランナー（`scripts/podctl-runner.sh`、`start.sh` が起動）が実行して、出力を同じフォルダに書きます。ジョブのフォルダは終了の1日後にランナーが消します。

```bash
python3 tools/podctl.py status
python3 tools/podctl.py start          # 既にあれば作らない。JupyterLabが応答するまで待つ
python3 tools/podctl.py start --cpu    # GPUなしの安いPod（$0.06/時ほど）。メインページ・ファイルの確認用
python3 tools/podctl.py exec 'nvidia-smi'
python3 tools/podctl.py upload ./train.sh voice-lab/setup/train.sh   # 送り先のフォルダは自動で作る
python3 tools/podctl.py download voice-lab/generated/out.wav ./out.wav
python3 tools/podctl.py stop --yes     # Podを削除（/workspace は残る）
```

クラウド環境の設定（Network secrets。キーはセッションから見えず、通信時に付けられる）：

| 名前 | Allowed websites | ヘッダー |
| --- | --- | --- |
| RunPod API | `rest.runpod.io` | `Authorization`、Prefix `Bearer`、Value はRunPodのAPIキー（この用途専用） |
| RunPod Jupyter | `*.proxy.runpod.net` | `Authorization`、Prefix `token`、Value はJupyterLabのパスワード |

JupyterLabのパスワードは、RunPodのテンプレートの環境変数 `JUPYTER_PASSWORD` と同じ値にします。Network secretsを使えない場合は、環境変数 `RUNPOD_API_KEY`・`JUPYTER_PASSWORD` でも動きます。

- Pod名は `runpod.ps1` と同じ（`ComfyUI-session-auto`）なので、PCとクラウドのどちらからでも同じPodを扱えます。
- `exec` は既定で600秒で打ち切ります（Pod内のコマンドは動き続け、出力は `/workspace/logs/podctl/<id>/out.log` に残る）。学習など長い処理は `nohup ... > /workspace/logs/xxx.log 2>&1 &` で起動し、ログを見ます。
- `--cpu` のPodは 2 vCPU・メモリ4GBで、名前が別（`Hub-cpu-auto`）なのでGPUのPodと同時に動かせます。`exec`・`upload`・`download`・`stop` も `--cpu` を付けるとこのPodを扱います。開くポートはメインページ（8000）・ファイルブラウザ（8080）・JupyterLab（8888）で、GPUを使う処理（学習や生成）はできません。
- GPUは `--gpu "NVIDIA RTX A5000" "NVIDIA GeForce RTX 3090"` のように複数指定でき、空きのあるものが使われます。

## メインページ（scripts/hub/）

CPUのPod（`podctl start --cpu`、PCからは `runpod.ps1 start -Cpu`）で開く、Pod内だけで動くページです。中身はPodの外に送りません。パスワードはファイルブラウザと同じ（`FILEBROWSER_PASSWORD`。`HUB_PASSWORD` があればそちら）。

- 結果：`/workspace/*-lab/generated/<実行>/` の音声・画像を一覧にし、ファイルごとに条件を並べて比べたり、条件ごとに評価（本人らしさ・自然さ・感情表現、読み間違い、メモ、☆）したりする。評価は `/workspace/hub/ratings.json` に保存。文の本文は、実行フォルダの `sentences.tsv` か `<分野>-lab/setup/compare_sentences.tsv`（タグ・指示・本文のタブ区切り）があれば表示する。
- 確認：`/workspace/logs/` と `/workspace/*-lab/logs/` のログの末尾、フォルダごとの容量。
- 作業：`/workspace/hub/tasks/*.sh` をボタンで実行する。スクリプトの先頭に次のように書くと入力欄ができ、値は環境変数 `HUB_<変数名>` で渡る。出力は `/workspace/hub/jobs/<id>/`。

```bash
# title: 作業の名前
# desc: 説明
# param: URLS | URL（1行に1つ） | textarea
```

GPUのPodでも動いていますが、テンプレートのポートに8000番が無いので外からは開けません。
