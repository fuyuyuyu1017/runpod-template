#!/usr/bin/env python3
"""生成した音声のフォルダから、試聴ページ（tools/listen_page.html）に載せる実験データを作る。

使い方:
  python3 tools/listen_page.py 元フォルダ 出力フォルダ --id 実験ID --title 表示名 [オプション]

元フォルダの形:
  元フォルダ/<条件>/<NN>_<タグ>.wav      条件は何階層でもよい（baseline/sbv2 なども可）
  sentences.tsv（任意）                  タグ<TAB>話し方の指示<TAB>本文（2列なら タグ<TAB>本文）
  results*.tsv（任意、どこにあってもよい） file 列（その TSV からの相対パス）と seconds・rtf・status など
ファイル名（拡張子を除く）が同じものを「同じ文」として横に並べる。

出力:
  出力フォルダ/data/<実験ID>/manifest.json
  出力フォルダ/data/<実験ID>/audio/<条件>/<NN>_<タグ>.mp3

主なオプション:
  --sentences TSV         文の一覧（省略時は元フォルダの sentences.tsv）
  --conditions a,b,c      並べる条件と順番（条件は元フォルダからの相対パス。省略時はすべて名前順）
  --label 条件=表示名     条件の表示名（何度でも指定できる。省略時はフォルダ名）
  --note 条件=説明        条件の説明（同上）
  --format mp3|wav        mp3 に変換する（既定）か、そのまま使うか
公開の手順は README の「音声の試聴ページ」を参照。
"""
import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus"}
# 公開ページのデータベースで使える文字（ID に使う）
SAFE = re.compile(r"[^A-Za-z0-9_.~:@+-]")


def die(msg):
    print(f"[listen_page] {msg}", file=sys.stderr)
    sys.exit(1)


def safe_id(text):
    return SAFE.sub("-", text).strip("-.") or "x"


def read_tsv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return [row for row in csv.reader(f, delimiter="\t") if row and any(c.strip() for c in row)]


def load_sentences(path):
    rows = read_tsv(path)
    if rows and rows[0][0].strip().lower() == "tag":
        rows = rows[1:]
    out = {}
    for row in rows:
        tag = row[0].strip()
        if len(row) >= 3:
            out[tag] = {"caption": row[1].strip(), "text": row[2].strip()}
        elif len(row) == 2:
            out[tag] = {"caption": "", "text": row[1].strip()}
    return out


def load_results(src):
    """results*.tsv をすべて読み、音声ファイルの絶対パス → 行 の辞書にする。"""
    out = {}
    for tsv in sorted(src.rglob("results*.tsv")):
        rows = read_tsv(tsv)
        if not rows or "file" not in rows[0]:
            continue
        head = rows[0]
        for row in rows[1:]:
            rec = dict(zip(head, row))
            out[(tsv.parent / rec.pop("file")).resolve()] = rec
    return out


def duration(path):
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True)
        return round(float(res.stdout.strip()), 2)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return None


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pairs(values, what):
    out = {}
    for v in values:
        if "=" not in v:
            die(f"{what} は 条件=値 の形で指定する: {v}")
        k, v = v.split("=", 1)
        out[k.strip().strip("/")] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser(description="試聴ページ用の実験データを作る")
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--id", required=True, help="実験ID（英数字とハイフン。例: irodori-si-20261027）")
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--sentences", type=Path)
    ap.add_argument("--conditions")
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--format", choices=["mp3", "wav"], default="mp3")
    args = ap.parse_args()

    src = args.src.resolve()
    if not src.is_dir():
        die(f"フォルダがない: {src}")
    exp_id = safe_id(args.id)
    labels, notes = pairs(args.label, "--label"), pairs(args.note, "--note")

    # 条件 = 音声ファイルを直接含むフォルダ
    by_dir = {}
    for f in sorted(src.rglob("*")):
        if f.is_file() and f.suffix.lower() in AUDIO_EXT:
            by_dir.setdefault(f.parent.relative_to(src).as_posix(), []).append(f)
    if not by_dir:
        die(f"音声ファイルが見つからない: {src}")
    if args.conditions:
        order = [c.strip().strip("/") for c in args.conditions.split(",") if c.strip()]
        missing = [c for c in order if c not in by_dir]
        if missing:
            die(f"条件が見つからない: {', '.join(missing)}（あるもの: {', '.join(by_dir)}）")
    else:
        order = sorted(by_dir)

    sentences_path = args.sentences or (src / "sentences.tsv")
    sentences = load_sentences(sentences_path) if sentences_path.is_file() else {}
    results = load_results(src)

    base_names = [Path(c).name for c in order]
    out_dir = args.out.resolve() / "data" / exp_id
    if out_dir.exists():
        shutil.rmtree(out_dir)
    conditions, clips, keys = [], {}, {}
    for rel in order:
        cond_id = safe_id(rel.replace("/", "__"))
        name = Path(rel).name
        conditions.append({
            "id": cond_id,
            "path": rel,
            "label": labels.get(rel) or labels.get(name) or (name if base_names.count(name) == 1 else rel),
            "note": notes.get(rel) or notes.get(name) or "",
        })
        for f in by_dir[rel]:
            key = f.stem
            keys.setdefault(key, None)
            ext = ".mp3" if args.format == "mp3" else f.suffix.lower()
            dest = out_dir / "audio" / cond_id / (safe_id(key) + ext)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if args.format == "mp3" and f.suffix.lower() != ".mp3":
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(f), "-codec:a", "libmp3lame",
                                "-q:a", "2", str(dest)], check=True)
            else:
                shutil.copyfile(f, dest)
            rec = results.get(f.resolve(), {})
            clip = {"src": dest.relative_to(args.out.resolve()).as_posix()}
            seconds = number(rec.get("seconds")) or duration(f)
            if seconds is not None:
                clip["seconds"] = seconds
            for k in ("rtf", "gen_sec"):
                if number(rec.get(k)) is not None:
                    clip[k] = number(rec[k])
            if rec.get("status"):
                clip["status"] = rec["status"]
            clips[f"{cond_id}/{key}"] = clip

    items = []
    for key in sorted(keys):
        tag = re.sub(r"^\d+[_-]", "", key)
        s = sentences.get(tag) or sentences.get(key) or {}
        items.append({"key": key, "tag": tag, "text": s.get("text", ""), "caption": s.get("caption", "")})

    manifest = {
        "id": exp_id,
        "title": args.title,
        "description": args.description,
        "created": date.today().isoformat(),
        "conditions": conditions,
        "items": items,
        "clips": clips,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in files)
    print(f"実験 {exp_id}: 条件 {len(conditions)}・文 {len(items)}・音声 {len(clips)}"
          f"（{len(files)} ファイル、{size / 1e6:.1f} MB）→ {out_dir}")
    if not sentences:
        print(f"注意: 文の一覧がない（{sentences_path}）。本文なしで表示する。")
    if len(files) > 250:
        print("注意: 1回の公開は255ファイルまで。数回に分けて公開する。")
    print("実験一覧への登録（ArtifactData set）:")
    print(json.dumps({"collection": "experiments", "doc_id": exp_id,
                      "data": {"title": args.title, "created": manifest["created"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
