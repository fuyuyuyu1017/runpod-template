#!/usr/bin/env bash
# Network Volume の容量内訳を表示する（読むだけ。何も削除・変更しない）。
# 使い方: bash /opt/runpod-template/scripts/disk-audit.sh
set -uo pipefail
VL=/workspace/voice-lab

show() {  # 指定フォルダの直下を大きい順に表示
  local d="$1" n="${2:-15}"
  [[ -d "$d" ]] || return 0
  echo; echo "=== $d ==="
  du -sh "$d"/* "$d"/.[!.]* 2>/dev/null | sort -rh | head -n "$n"
}

echo "=== /workspace 全体 ==="
du -sh /workspace 2>/dev/null
show /workspace 20
show /workspace/apps
show $VL 20
show $VL/venvs
show $VL/apps
show $VL/cache/huggingface/hub 20
show $VL/generated 20
show $VL/checkpoints
show $VL/dataset
show $VL/models
show $VL/backups

echo; echo "=== 学習の途中保存（apps 内の .pth / .safetensors / .ckpt、上位20件） ==="
find $VL/apps -type f \( -name '*.pth' -o -name '*.safetensors' -o -name '*.ckpt' \) \
  -size +50M -printf '%s\t%TY-%Tm-%Td\t%p\n' 2>/dev/null \
  | sort -rn | head -20 | awk -F'\t' '{printf "%6.1fG  %s  %s\n", $1/1073741824, $2, $3}'

echo; echo "=== pip のキャッシュ（Volume 上にあれば） ==="
du -sh /workspace/.cache /root/.cache 2>/dev/null
