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

Claude Code on the web などのクラウド環境から、Podの作成・コマンド実行・ファイル転送・削除をするツールです。クラウド環境からはSSHもWebSocket（JupyterLabのターミナル）も使えないため、通信はすべてJupyterLab（8888番）へのHTTPSで行います。`exec` はコマンドを `/workspace/logs/podctl/<id>/cmd.sh` に置き、Pod内のランナー（`scripts/podctl-runner.sh`、`start.sh` が起動）が実行して、出力を同じフォルダに書きます。ジョブのフォルダは終了の1日後にランナーが消します。

```bash
python3 tools/podctl.py status
python3 tools/podctl.py start          # 既にあれば作らない。JupyterLabが応答するまで待つ
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
- GPUは `--gpu "NVIDIA RTX A5000" "NVIDIA GeForce RTX 3090"` のように複数指定でき、空きのあるものが使われます。

## 音声の試聴ページ（tools/listen_page.py）

音声生成AIの成果物を実験ごとに並べて聞き比べる公開ページです（[音声試聴ルーム](https://claude.ai/artifact/EaVuDgebxJRvgFq7ifrcPS)、非公開）。同じ文を条件ごとに横に並べて再生し、文ごとのベスト（☆）と条件ごとの評価（本人らしさ・自然さ・感情表現・読み間違い・メモ）を残せます。評価はページのデータベースに保存され、Claudeからも読めます。

ページ本体は `tools/listen_page.html`。音声はページと一緒に `data/<実験ID>/` に公開し、実験の一覧はデータベースの `experiments` に置きます。実験を足すとき（Claudeが行う）：

1. 生成した音声をクラウド環境に持ってくる（`podctl download` やライブラリの `<テーマ>/listen/`）。形は `<元フォルダ>/<条件>/<NN>_<タグ>.wav`。ファイル名が同じものが同じ文として並ぶ。
2. 実験データを作る（mp3に変換し、`results*.tsv` の秒数・RTF・失敗を拾う）：
   ```bash
   python3 tools/listen_page.py 元フォルダ 出力フォルダ --id irodori-si-20261027 --title "表示名" \
     --sentences compare_sentences.tsv --label s0500="学習 500ステップ" --note s0500="説明"
   ```
3. Artifactツールで `tools/listen_page.html` を上のURLに公開し直す。`root` を出力フォルダ、`files` を `data/<実験ID>/` の全ファイルにする（1回255ファイルまで。前に公開した実験のファイルは残る）。
4. ArtifactDataで `experiments/<実験ID>` に `{title, created}` を書く（スクリプトが最後に表示する）。

評価の読み出し：`experiments/<実験ID>/ratings`（条件ごと）、`experiments/<実験ID>/picks`（文ごとのベスト）。
