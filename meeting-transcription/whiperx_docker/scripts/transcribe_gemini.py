#!/usr/bin/env python3
"""
transcribe_gemini.py - 使用 Google Gemini API 進行語音轉錄 + 語者分離

功能：
    - 轉錄音訊為文字（Gemini multimodal，支援中文）
    - 以 AI 推斷辨識並區分不同說話者（Speaker 1, Speaker 2, ...）
    - 輸出帶有語者標籤的逐字稿（格式與 transcribe_diarize.py 相同）

Usage:
    python3 transcribe_gemini.py <audio_file> [--output-dir <dir>] [--lang <language>]
    python3 transcribe_gemini.py <audio_file> [--model gemini-2.5-pro]

Output:
    <output_dir>/<basename>/<basename>_逐字稿.md

==============================================================
安裝步驟（首次使用）：
==============================================================
1. 安裝 Python 套件：
       pip install google-generativeai mutagen

2. 申請 Gemini API Key（免費）：
       https://aistudio.google.com/app/apikey

3. 設定環境變數：
       export GEMINI_API_KEY=AIza...

注意：
    - 語者分離為 AI 語意推斷，非聲學分析，準確度因錄音品質而異
    - 時間戳記為 Gemini 估算，可能有誤差
    - 音訊上傳至 Google 伺服器處理，請注意資料隱私
==============================================================
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_audio_duration(audio_path: Path) -> float:
    """取得音訊時長（秒），需要 mutagen 套件。"""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(audio_path))
        if audio and audio.info:
            return float(audio.info.length)
    except Exception:
        pass
    return 0.0


def parse_gemini_transcript(raw_text: str) -> list[dict]:
    """
    解析 Gemini 輸出的逐字稿，格式：
        [HH:MM:SS] Speaker N: 文字內容

    回傳 list of dict：
        {"speaker": "Speaker 1", "timestamp": "00:01:23", "text": "..."}
    """
    segments = []
    # 嘗試解析 [HH:MM:SS] Speaker N: text 格式
    pattern = re.compile(
        r"\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*(Speaker\s*\d+)\s*[:：]\s*(.+)"
    )
    # 也嘗試無時間戳記格式：Speaker N: text
    pattern_no_ts = re.compile(
        r"(Speaker\s*\d+)\s*[:：]\s*(.+)"
    )

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        m = pattern.match(line)
        if m:
            segments.append({
                "timestamp": m.group(1),
                "speaker": m.group(2).strip(),
                "text": m.group(3).strip(),
            })
            continue

        m2 = pattern_no_ts.match(line)
        if m2:
            segments.append({
                "timestamp": None,
                "speaker": m2.group(1).strip(),
                "text": m2.group(2).strip(),
            })

    return segments


def build_transcript_md(segments: list[dict], audio_name: str, language: str, duration: float, raw_text: str) -> str:
    """產生帶語者標籤的 Markdown 逐字稿。"""
    # 統計語者人數
    speakers = {seg["speaker"] for seg in segments}
    num_speakers = len(speakers) if speakers else "不明"

    lines = [
        f"# 逐字稿 - {audio_name}",
        "",
        f"**語言:** {language}",
        f"**總時長:** {format_timestamp(duration) if duration else '不明'}",
        f"**語者人數:** {num_speakers}",
        f"**轉錄引擎:** Google Gemini",
        "",
        "---",
        "",
    ]

    if not segments:
        # 解析失敗，直接輸出原文
        lines.append("> ⚠️ 無法解析語者標籤，輸出原始轉錄結果：")
        lines.append("")
        lines.append(raw_text)
        return "\n".join(lines)

    # 合併同一語者的連續段落
    prev_speaker = None
    buffer_texts = []
    buffer_ts = None

    def flush(spk, texts, ts):
        if not texts:
            return
        ts_str = f"[{ts}] " if ts else ""
        lines.append(f"**{ts_str}{spk}:**")
        lines.append(" ".join(texts))
        lines.append("")

    for seg in segments:
        spk = seg["speaker"]
        txt = seg["text"]
        ts = seg.get("timestamp")

        if spk == prev_speaker:
            buffer_texts.append(txt)
        else:
            flush(prev_speaker, buffer_texts, buffer_ts)
            prev_speaker = spk
            buffer_texts = [txt]
            buffer_ts = ts

    flush(prev_speaker, buffer_texts, buffer_ts)

    return "\n".join(lines)


def transcribe_with_gemini(
    audio_path: Path,
    language: str = "zh",
    model_name: str = "gemini-2.0-flash",
) -> tuple[str, str]:
    """
    使用 Gemini API 轉錄音訊並進行語者分離。
    回傳 (原始回應文字, 使用的語言名稱)
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY 環境變數未設定。", file=sys.stderr)
        print("       請至 https://aistudio.google.com/app/apikey 申請 API Key", file=sys.stderr)
        sys.exit(1)

    try:
        import google.generativeai as genai
    except ImportError:
        print("ERROR: google-generativeai 未安裝。請執行：pip install google-generativeai", file=sys.stderr)
        sys.exit(1)

    genai.configure(api_key=api_key)

    # 語言對應中文名稱（用於 prompt）
    lang_name_map = {
        "zh": "繁體中文", "zh-tw": "繁體中文", "zh-cn": "簡體中文",
        "en": "英文", "ja": "日文", "ko": "韓文", "auto": "自動偵測",
    }
    lang_display = lang_name_map.get(language.lower(), language)
    lang_instruction = f"請以{lang_display}輸出逐字稿。" if language != "auto" else "請以音訊的原始語言輸出逐字稿。"

    prompt = f"""請將這段音訊轉錄為逐字稿，並辨識不同的說話者。

{lang_instruction}

輸出格式要求（每行一句，嚴格遵守）：
[HH:MM:SS] Speaker N: 說話內容

規則：
- 相同的人聲必須使用相同的 Speaker 編號（如 Speaker 1、Speaker 2）
- 說話者改變時才換行並更新 Speaker 編號
- 時間戳記使用該句開始的時間，格式 HH:MM:SS
- 保留原始發音，不要修改或省略任何內容
- 不要輸出任何說明文字，只輸出逐字稿

範例：
[00:00:01] Speaker 1: 大家好，今天我們來討論第三季的業績報告。
[00:00:08] Speaker 2: 好的，我先說一下目前的狀況。
[00:00:15] Speaker 1: 請繼續。"""

    print(f"上傳音訊至 Gemini（{audio_path.name}）...")
    audio_file = genai.upload_file(str(audio_path))

    # 等待檔案處理完成
    print("等待 Gemini 處理音訊...")
    while audio_file.state.name == "PROCESSING":
        time.sleep(2)
        audio_file = genai.get_file(audio_file.name)

    if audio_file.state.name == "FAILED":
        print("ERROR: Gemini 音訊處理失敗。", file=sys.stderr)
        sys.exit(1)

    print(f"轉錄中（模型：{model_name}）...")
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(
        [audio_file, prompt],
        generation_config={"temperature": 0.1},
    )

    # 清理上傳的檔案（非必要，但節省配額）
    try:
        genai.delete_file(audio_file.name)
    except Exception:
        pass

    return response.text, language


def main():
    parser = argparse.ArgumentParser(
        description="使用 Google Gemini API 轉錄音訊 + 語者分離，輸出帶 Speaker 標籤的 Markdown 逐字稿。"
    )
    parser.add_argument("audio_file", help="音訊檔路徑（mp3, mp4, wav, m4a 等）")
    parser.add_argument("--output-dir", default=".", help="輸出根目錄（預設：當前目錄）")
    parser.add_argument("--lang", default="zh", help="語言代碼（預設：zh；設 auto 自動偵測）")
    parser.add_argument(
        "--model", default="gemini-2.5-flash",
        help="Gemini 模型（預設：gemini-2.5-flash；高品質可用 gemini-2.5-pro）"
    )
    args = parser.parse_args()

    audio_path = Path(args.audio_file).resolve()
    if not audio_path.exists():
        print(f"ERROR: 找不到檔案：{audio_path}", file=sys.stderr)
        sys.exit(1)

    basename = audio_path.stem
    output_dir = Path(args.output_dir) / basename
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"建立資料夾: {output_dir}")

    duration = get_audio_duration(audio_path)
    if duration:
        print(f"音訊時長: {format_timestamp(duration)}")

    raw_text, language = transcribe_with_gemini(
        audio_path,
        language=args.lang,
        model_name=args.model,
    )

    print("解析語者標籤...")
    segments = parse_gemini_transcript(raw_text)
    print(f"解析完成，共 {len(segments)} 段")

    md_content = build_transcript_md(segments, basename, language, duration, raw_text)

    transcript_path = output_dir / f"{basename}_逐字稿.md"
    transcript_path.write_text(md_content, encoding="utf-8")

    speakers = sorted({seg["speaker"] for seg in segments})
    print("\n完成！")
    print(f"  逐字稿: {transcript_path}")
    if speakers:
        print(f"\n語者列表：{', '.join(speakers)}")
    print("\n提示：語者代號為 AI 推斷，請人工確認是否對應正確。")


if __name__ == "__main__":
    main()
