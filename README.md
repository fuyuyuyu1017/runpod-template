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
