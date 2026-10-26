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
  FB_PASS="${FILEBROWSER_PASSWORD:-$(random_password)}"
  filebrowser -d "$FB_DB" config init >/dev/null
  filebrowser -d "$FB_DB" users add admin "$FB_PASS" --perm.admin >/dev/null
  echo "[start] ファイルブラウザ: 初回設定（ユーザー admin / パスワード: $FB_PASS）"
fi
nohup filebrowser -d "$FB_DB" -r /workspace -a 0.0.0.0 -p 8080 \
  >> "$LOG_DIR/filebrowser.log" 2>&1 &
echo "[start] ファイルブラウザ: 起動（ログ: $LOG_DIR/filebrowser.log）"

# --- ComfyUI（8188番）。Volume上にインストール済みのときだけ ---
if [[ -x "$COMFY_DIR/.venv/bin/python" ]]; then
  (
    cd "$COMFY_DIR" || exit 1
    nohup env -u LD_LIBRARY_PATH .venv/bin/python main.py --listen 0.0.0.0 --port 8188 \
      >> "$LOG_DIR/comfyui.log" 2>&1 &
  )
  echo "[start] ComfyUI: 起動（ログ: $LOG_DIR/comfyui.log）"
else
  echo "[start] ComfyUI: 未インストール。bash /opt/runpod-template/scripts/install-comfyui.sh で入れられます"
fi

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
