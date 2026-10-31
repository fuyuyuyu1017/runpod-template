#!/usr/bin/env bash
# ComfyUI を起動する（start.sh が GPU の Pod で呼ぶ。手で起動し直すときも使える）。
# ComfyUI 本体は Pod の中（127.0.0.1:8189）だけで待ち受け、外（RunPod のプロキシ）からは
# パスワード付きの入口（nginx、8188番）を通したときだけ開ける。
# パスワードは COMFYUI_PASSWORD、無ければ JUPYTER_PASSWORD。どちらも無ければ外には出さない。
#   bash start-comfyui.sh          # GPU で起動
#   bash start-comfyui.sh --cpu    # CPU で起動（動作の確認用）
set -uo pipefail

COMFY_DIR=/workspace/apps/ComfyUI
LOG_DIR=/workspace/logs
PUBLIC_PORT="${COMFYUI_PUBLIC_PORT:-8188}"
INTERNAL_PORT="${COMFYUI_INTERNAL_PORT:-8189}"
GATE_USER="${COMFYUI_USER:-ailab}"
# パスワードのハッシュは Volume に置かない（Pod と一緒に消える場所）
GATE_DIR=/run/comfyui-gate
mkdir -p "$LOG_DIR"

if [[ ! -x "$COMFY_DIR/.venv/bin/python" ]]; then
  echo "[comfyui] 未インストール。bash /opt/runpod-template/scripts/install-comfyui.sh で入れられます"
  exit 1
fi

extra=()
[[ "${1:-}" == "--cpu" ]] && extra+=(--cpu)

if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$INTERNAL_PORT/"; then
  echo "[comfyui] 本体はもう動いています（127.0.0.1:$INTERNAL_PORT）"
else
  (
    cd "$COMFY_DIR" || exit 1
    # --enable-cors-header: RunPodの中継経由のアクセスを、ComfyUIの送信元チェックで403にしないため
    nohup env -u LD_LIBRARY_PATH .venv/bin/python main.py --listen 127.0.0.1 --port "$INTERNAL_PORT" \
      --enable-cors-header "${extra[@]}" >> "$LOG_DIR/comfyui.log" 2>&1 &
  )
  echo "[comfyui] 本体: 起動（Pod の中だけ 127.0.0.1:$INTERNAL_PORT、ログ: $LOG_DIR/comfyui.log）"
fi

password="${COMFYUI_PASSWORD:-${JUPYTER_PASSWORD:-}}"
if [[ -z "$password" ]]; then
  echo "[comfyui] COMFYUI_PASSWORD・JUPYTER_PASSWORD が未設定のため、外からは開けないままにします"
  exit 0
fi

# --- パスワード付きの入口（nginx）。値はログに出さない ---
mkdir -p "$GATE_DIR" && chmod 700 "$GATE_DIR"
umask 077
printf '%s:%s\n' "$GATE_USER" "$(openssl passwd -apr1 -stdin <<<"$password")" > "$GATE_DIR/htpasswd"
unset password
cat > "$GATE_DIR/nginx.conf" <<EOF
user root;
worker_processes 1;
pid $GATE_DIR/nginx.pid;
error_log $LOG_DIR/comfyui-gate.log warn;
events { worker_connections 256; }
http {
  access_log off;
  client_max_body_size 0;
  map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
  server {
    listen $PUBLIC_PORT;
    auth_basic "ComfyUI";
    auth_basic_user_file $GATE_DIR/htpasswd;
    location / {
      proxy_pass http://127.0.0.1:$INTERNAL_PORT;
      proxy_http_version 1.1;
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection \$connection_upgrade;
      proxy_set_header Host \$host;
      proxy_set_header Authorization "";
      proxy_buffering off;
      proxy_read_timeout 1d;
      proxy_send_timeout 1d;
    }
  }
}
EOF
if [[ -f "$GATE_DIR/nginx.pid" ]] && kill -0 "$(cat "$GATE_DIR/nginx.pid")" 2>/dev/null; then
  nginx -c "$GATE_DIR/nginx.conf" -s reload && echo "[comfyui] 入口: 設定を読み直しました（$PUBLIC_PORT番）"
else
  nginx -c "$GATE_DIR/nginx.conf" \
    && echo "[comfyui] 入口: 起動（$PUBLIC_PORT番、ユーザー名 $GATE_USER とパスワードが必要。ログ: $LOG_DIR/comfyui-gate.log）" \
    || echo "[comfyui] 入口: 起動に失敗（$LOG_DIR/comfyui-gate.log）"
fi
