#!/usr/bin/env python3
# 写し：正本は base-library の ops/path-check/check_paths.py（2026-11-05 に写した。直すときは正本から写し直す）
"""PC の絶対パスの直書きを探す（docs/common/paths.md 6章・dev.md 8章）。標準ライブラリだけ。読むだけで何も直さない。

  python3 ops/path-check/check_paths.py <リポジトリのフォルダ> [...]      # コードだけ
  python3 ops/path-check/check_paths.py --docs <フォルダ> [...]           # 文書（.md）の数も出す

- 見つけるもの：ドライブ文字から始まるパス（D:\\…、C:/…）と、ユーザーのホーム（C:\\Users\\<名前>、/home/<名前>、/Users/<名前>）。 path-default
- 許すもの：同じ行に印 `path-default` があるもの（既定値を書いてよい1か所。paths.md 3章）。
- 分け方：コードの行＝直す対象（NG）。コメントだけの行＝使い方の説明（WARN。動きには効かないが古くなる）。
- 見ないもの：過去の記録（tasks/_done/・notes/・projects/・_archive/）、.git、圧縮ファイル。
- 出すのはファイル名・行番号・見つけたパスだけ（行の中身は出さない）。
終わりのコード：NG があれば 1、なければ 0。
"""
import re
import sys
from pathlib import Path

CODE_EXT = {".py", ".ps1", ".psm1", ".psd1", ".sh", ".bat", ".cmd", ".js", ".mjs", ".ts", ".html",
            ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".service", ".gd", ".cs"}
SKIP_DIRS = {".git", "_done", "notes", "projects", "_archive", "node_modules", "__pycache__", ".venv", "venv"}
SKIP_FILES = {"tasks-index.json", "research-index.json"}  # 文書から作る索引（元の文書の側で数える）
PAT = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]:(?:\\\\|\\|/)[^\s\"'`<>|,;)]*|/(?:home|Users)/[A-Za-z0-9_.-]+[^\s\"'`<>|,;)]*)")
COMMENT = re.compile(r"^\s*(#|//|REM\b|rem\b|::|<!--|\*|;)")
ALLOW = "path-default"


def scan(root: Path, docs: bool):
    out = {"NG": [], "WARN": [], "DOC": 0}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        if p.name in SKIP_FILES:
            continue
        ext = p.suffix.lower()
        is_doc = ext == ".md"
        if not (ext in CODE_EXT or (docs and is_doc)):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            hits = PAT.findall(line)
            if not hits or ALLOW in line:
                continue
            if is_doc:
                out["DOC"] += len(hits)
                continue
            kind = "WARN" if COMMENT.match(line) else "NG"
            out[kind].append(f"{p.relative_to(root)}:{n}  {hits[0][:80]}")
    return out


def main(argv):
    docs = "--docs" in argv
    roots = [Path(a) for a in argv if a != "--docs"]
    if not roots:
        print(__doc__)
        return 2
    bad = 0
    for r in roots:
        res = scan(r, docs)
        bad += len(res["NG"])
        line = f"== {r.resolve().name}: NG {len(res['NG'])} / WARN {len(res['WARN'])}"
        print(line + (f" / 文書の中 {res['DOC']}" if docs else ""))
        for k in ("NG", "WARN"):
            for h in res[k]:
                print(f"  {k}: {h}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
