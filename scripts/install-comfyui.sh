#!/usr/bin/env bash
# 新しいComfyUIをNetwork Volume上にインストールする（初回だけ実行）。
# 既存の /workspace/runpod-slim/ComfyUI には触れず、モデルフォルダだけを参照する。
# 再実行すると、最新のリリースへ更新する。
set -Eeuo pipefail

COMFY_DIR="${COMFY_DIR:-/workspace/apps/ComfyUI}"
OLD_MODELS="${OLD_MODELS:-/workspace/runpod-slim/ComfyUI/models}"
COMFY_REF="${COMFY_REF:-latest}"   # latest = 最新のリリースタグ。ブランチ名やタグも指定できる
PY=/usr/bin/python3.12
TORCH_INDEX="https://download.pytorch.org/whl/cu128"

trap 'echo "NG: ${LINENO}行目で止まりました"' ERR
unset VIRTUAL_ENV PYTHONPATH LD_LIBRARY_PATH

echo "=== 1. ComfyUIの取得 ==="
mkdir -p "$(dirname "$COMFY_DIR")"
if [[ ! -d "$COMFY_DIR/.git" ]]; then
  git clone https://github.com/Comfy-Org/ComfyUI.git "$COMFY_DIR"
fi
cd "$COMFY_DIR"
git fetch --tags origin
if [[ "$COMFY_REF" == "latest" ]]; then
  COMFY_REF="$(git tag --list 'v*' --sort=-v:refname | head -1)"
fi
git checkout "$COMFY_REF"
echo "バージョン: $COMFY_REF"

echo "=== 2. 仮想環境とパッケージ ==="
[[ -x .venv/bin/python ]] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install torch torchvision torchaudio --index-url "$TORCH_INDEX"
.venv/bin/pip install -r requirements.txt

echo "=== 3. ComfyUI-Manager ==="
MANAGER_DIR=custom_nodes/ComfyUI-Manager
if [[ ! -d "$MANAGER_DIR/.git" ]]; then
  git clone https://github.com/ltdrdata/ComfyUI-Manager.git "$MANAGER_DIR"
fi
.venv/bin/pip install -r "$MANAGER_DIR/requirements.txt"

echo "=== 4. 既存のモデルフォルダを参照 ==="
if [[ -d "$OLD_MODELS" && ! -f extra_model_paths.yaml ]]; then
  cat > extra_model_paths.yaml <<EOF
# 既存ComfyUIのモデルを、コピーせずに読み込む
existing:
  base_path: $OLD_MODELS
  checkpoints: checkpoints
  loras: loras
  vae: vae
  clip: clip
  text_encoders: text_encoders
  clip_vision: clip_vision
  diffusion_models: diffusion_models
  unet: unet
  controlnet: controlnet
  upscale_models: upscale_models
  embeddings: embeddings
EOF
  echo "作成: $COMFY_DIR/extra_model_paths.yaml（参照先: $OLD_MODELS）"
else
  echo "スキップ（参照先がない、またはすでに設定済み）"
fi

echo "=== 5. 確認 ==="
.venv/bin/python -c 'import torch; print("torch", torch.__version__, "/ GPU使用可:", torch.cuda.is_available())'
echo
echo "完了しました。Podを作り直すと自動で起動します。今すぐ起動するには:"
echo "  cd $COMFY_DIR && nohup .venv/bin/python main.py --listen 0.0.0.0 --port 8188 --enable-cors-header >> /workspace/logs/comfyui.log 2>&1 &"
