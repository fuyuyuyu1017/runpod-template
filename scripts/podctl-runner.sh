#!/usr/bin/env bash
# tools/podctl.py の exec 用。クラウド環境の通信はWebSocketを通さないため、
# コマンドはファイルで受け渡す：podctl が JOBS/<id>/cmd.sh を置くと、ここで実行して
# 出力を out.log、終了コードを exit に書く。podctl はそれをHTTPSで読む。
set -uo pipefail

JOBS=/workspace/logs/podctl
mkdir -p "$JOBS"

# 二重起動を防ぐ（start.sh と startup.d の両方から起動されても1つだけ動く）
exec 9>/tmp/podctl-runner.lock
flock -n 9 || exit 0

while true; do
  for d in "$JOBS"/*/; do
    [[ -f "$d/cmd.sh" && ! -e "$d/started" ]] || continue
    touch "$d/started"
    (
      cd /workspace && bash "$d/cmd.sh" > "$d/out.log" 2>&1
      echo $? > "$d/exit.tmp" && mv "$d/exit.tmp" "$d/exit"
    ) 9>&- &
  done
  # 終わってから1日たったジョブを片付ける
  find "$JOBS" -mindepth 2 -maxdepth 2 -name exit -mmin +1440 -printf '%h\0' 2>/dev/null \
    | xargs -0 -r rm -rf
  sleep 1
done
