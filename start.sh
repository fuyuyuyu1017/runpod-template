#!/usr/bin/env bash
# コンテナ起動時に実行する。各サービスはバックグラウンドで起動し、1つが失敗しても他は止めない。
set -uo pipefail

LOG_DIR=/workspace/logs
COMFY_DIR=/workspace/apps/ComfyUI
mkdir -p "$LOG_DIR"

random_password() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20; }

# --- SSH（RunPodの設定に公開鍵があるときだけ） ---
if [[ -n "${PUBLIC_KEY:-}" ]]; then
  mkdir -p ~/.ssh
  echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys
  chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
  ssh-keygen -A >/dev/null
  /usr/sbin/sshd && echo "[start] SSH: 起動"
fi

# --- 外部サービスの設定（値は表示せず、設定の有無だけログに出す） ---
for v in HF_TOKEN CIVITAI_TOKEN; do
  [[ -n "${!v:-}" ]] && echo "[start] $v: 設定あり" || echo "[start] $v: 未設定"
done
if [[ -n "${RCLONE_CONFIG_OFFSITE_TYPE:-}" ]]; then
  echo "[start] 外部バックアップ（rclone の offsite:）: 設定あり（${RCLONE_CONFIG_OFFSITE_TYPE}）"
else
  echo "[start] 外部バックアップ（rclone の offsite:）: 未設定"
fi

# --- JupyterLab（8888番） ---
JUPYTER_TOKEN="${JUPYTER_PASSWORD:-}"
if [[ -z "$JUPYTER_TOKEN" ]]; then
  JUPYTER_TOKEN="$(random_password)"
  echo "[start] JUPYTER_PASSWORD が未設定のため、今回だけのトークンを作りました: $JUPYTER_TOKEN"
fi
nohup /opt/jupyter/bin/jupyter lab \
  --allow-root --no-browser --ip=0.0.0.0 --port=8888 \
  --ServerApp.root_dir=/workspace \
  --IdentityProvider.token="$JUPYTER_TOKEN" \
  --ServerApp.allow_origin='*' \
  --ServerApp.terminado_settings='{"shell_command":["/bin/bash"]}' \
  >> "$LOG_DIR/jupyter.log" 2>&1 &
echo "[start] JupyterLab: 起動（ログ: $LOG_DIR/jupyter.log）"

# --- ファイルブラウザ（8080番）。設定とユーザーはVolumeに保存して次回も使う ---
FB_DIR=/workspace/.filebrowser
FB_DB="$FB_DIR/filebrowser.db"
mkdir -p "$FB_DIR"
if [[ ! -f "$FB_DB" ]]; then
  FB_PASS="${FILEBROWSER_PASSWORD:-}"
  if [[ -z "$FB_PASS" ]]; then
    FB_PASS="$(random_password)"
    echo "[start] FILEBROWSER_PASSWORD が未設定のため、パスワードを作りました: $FB_PASS"
  elif (( ${#FB_PASS} < 12 )); then
    echo "[start] 注意: FILEBROWSER_PASSWORD は12文字以上にしてください（短いと作成に失敗します）"
  fi
  filebrowser -d "$FB_DB" config init >/dev/null
  # 失敗したらDBを消して、次回の起動でやり直せるようにする
  if filebrowser -d "$FB_DB" users add admin "$FB_PASS" --perm.admin >/dev/null; then
    echo "[start] ファイルブラウザ: 初回設定（ユーザー admin を作成）"
  else
    rm -f "$FB_DB"
    echo "[start] ファイルブラウザ: admin の作成に失敗しました。上のエラーを確認してください"
  fi
fi
if [[ -f "$FB_DB" ]]; then
  nohup filebrowser -d "$FB_DB" -r /workspace -a 0.0.0.0 -p 8080 \
    >> "$LOG_DIR/filebrowser.log" 2>&1 &
  echo "[start] ファイルブラウザ: 起動（ログ: $LOG_DIR/filebrowser.log）"
fi

# --- ComfyUI（8188番）。Volume上にインストール済みで、GPUがあるときだけ ---
if ! nvidia-smi -L >/dev/null 2>&1; then
  echo "[start] ComfyUI: GPUが無いPod（CPUのPod）なので起動しません"
elif [[ -x "$COMFY_DIR/.venv/bin/python" ]]; then
  (
    cd "$COMFY_DIR" || exit 1
    # --enable-cors-header: RunPodの中継経由のアクセスを、ComfyUIの送信元チェックで403にしないため
    nohup env -u LD_LIBRARY_PATH .venv/bin/python main.py --listen 0.0.0.0 --port 8188 --enable-cors-header \
      >> "$LOG_DIR/comfyui.log" 2>&1 &
  )
  echo "[start] ComfyUI: 起動（ログ: $LOG_DIR/comfyui.log）"
else
  echo "[start] ComfyUI: 未インストール。bash /opt/runpod-template/scripts/install-comfyui.sh で入れられます"
fi

# --- メインページ（8000番。CPUのPodで開く。結果の試聴・評価、ログ、決めた作業の実行） ---
nohup /usr/bin/python3.12 /opt/runpod-template/scripts/hub/hub.py >> "$LOG_DIR/hub.log" 2>&1 &
echo "[start] メインページ: 起動（ログ: $LOG_DIR/hub.log）"

# --- tools/podctl.py の exec 用ランナー ---
nohup bash /opt/runpod-template/scripts/podctl-runner.sh >> "$LOG_DIR/podctl-runner.log" 2>&1 &
echo "[start] podctlランナー: 起動"

# --- 追加の起動処理（/workspace/startup.d/*.sh があれば順に実行） ---
if [[ -d /workspace/startup.d ]]; then
  for f in /workspace/startup.d/*.sh; do
    [[ -f "$f" ]] || continue
    echo "[start] 追加処理: $f"
    bash "$f" >> "$LOG_DIR/startup.d.log" 2>&1 || echo "[start] 失敗: $f"
  done
fi

echo "[start] 起動処理が終わりました"
sleep infinity
