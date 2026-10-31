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
  # 値はログに出さない（/workspace/logs は Volume に残る）。ランダムな値で鍵をかけるので、誰も入れない
  echo "[start] JUPYTER_PASSWORD が未設定です。RunPod のテンプレートか環境変数に設定してください（今回は誰も入れない状態で起動します）"
fi
nohup /opt/jupyter/bin/jupyter lab \
  --allow-root --no-browser --ip=0.0.0.0 --port=8888 \
  --ServerApp.root_dir=/workspace \
  --IdentityProvider.token="$JUPYTER_TOKEN" \
  --ServerApp.allow_origin='*' \
  --ServerApp.terminado_settings='{"shell_command":["/bin/bash"]}' \
  >> "$LOG_DIR/jupyter.log" 2>&1 &
echo "[start] JupyterLab: 起動（ログ: $LOG_DIR/jupyter.log）"

# --- ComfyUI（8188番）。Volume上にインストール済みで、GPUがあるときだけ ---
# 本体は Pod の中だけで待ち受け、外からはパスワード付きの入口を通す（scripts/start-comfyui.sh）
if ! nvidia-smi -L >/dev/null 2>&1; then
  echo "[start] ComfyUI: GPUが無いPod（CPUのPod）なので起動しません"
else
  bash /opt/runpod-template/scripts/start-comfyui.sh
fi

# --- AILab ハブ（8000番。CPUのPodで開く。結果の試聴・評価、ログ、決めた作業の実行） ---
# 本体は別のリポジトリ ailab-hub。Network Volume の /workspace/hub/app/ に送ったものを起動する
HUB_APP=/workspace/hub/app/hub.py
if [[ -f "$HUB_APP" ]]; then
  nohup /usr/bin/python3.12 "$HUB_APP" >> "$LOG_DIR/hub.log" 2>&1 &
  echo "[start] AILab ハブ: 起動（ログ: $LOG_DIR/hub.log）"
else
  echo "[start] AILab ハブ: $HUB_APP がありません（ailab-hub の README の手順で送る）"
fi

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
