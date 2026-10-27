# Claude向けメモ

- ユーザーへの説明は日本語で行う。
- RunPodの操作は `tools/podctl.py` を使う（使い方はREADME）。認証はクラウド環境の Network secrets（または環境変数 `RUNPOD_API_KEY`・`JUPYTER_PASSWORD`）。
- 音声生成の成果物を聞き比べるときは、試聴ページ（`tools/listen_page.py`、READMEの「音声の試聴ページ」）に実験を追加する。
- Podは動いている間ずっと課金される。作業が終わったら `stop --yes` で削除し、`status` で残っていないことを確認する。学習中などで残すときは、ユーザーにそう伝える。
- `/workspace` 内の削除は、対象とサイズを示してユーザーの了承を得てから行う。ComfyUI関連（`/workspace/apps/ComfyUI`、`/workspace/runpod-slim`）とその仮想環境には触れない。
- Network Volumeは確保したサイズで課金され、縮小できない。容量を広げる前にユーザーに相談する。
