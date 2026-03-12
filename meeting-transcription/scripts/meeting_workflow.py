#!/usr/bin/env python3
"""
Meeting Transcription Workflow
支援兩種轉錄模式：
  --mode gemini  使用 Google Gemini API（預設）
  --mode local   使用本地 Whisper 伺服器

Usage:
  python3 meeting_workflow.py <audio_file> [--mode gemini|local] [--emails addr ...]

.env 環境變數：
  GEMINI_API_KEY, SMTP_USER, SMTP_PASS, EMAIL_RECIPIENTS
  LOCAL_SERVER_IP, LOCAL_SERVER_PORT, LOCAL_API_KEY
"""

import os
import sys
import re
import time
import argparse
import mimetypes
import smtplib
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from dotenv import load_dotenv
_workspace = Path(__file__).resolve().parents[2]
load_dotenv(_workspace / ".env")
load_dotenv(Path.home() / ".openclaw/workspace/.env")

import requests
import markdown as md_lib


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def check_env(*keys):
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        print(f"[錯誤] 缺少環境變數: {', '.join(missing)}")
        sys.exit(1)


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "audio/mpeg"


# ──────────────────────────────────────────────────────────
# Mode A: Gemini 轉錄
# ──────────────────────────────────────────────────────────

TRANSCRIPTION_PROMPT = """
你是一位專業的逐字稿整理員。
請將這段音訊內容轉成繁體中文逐字稿，格式要求如下：

1. 每段發言必須標記時間戳記，格式為 [HH:MM:SS]
2. 自動辨識不同說話者，依出場順序命名為 Speaker 1、Speaker 2、Speaker 3 …
3. 相同的人聲給予相同的代號（跨段落一致）
4. 每段格式如下（換行分段）：

[HH:MM:SS] Speaker N: 說話內容

5. 英文專有名詞、系統名稱、術語保留英文原文
6. 輸出純文字，不加任何額外說明或前言
"""

def transcribe_gemini(audio_path: Path, model: str) -> str:
    from google import genai as gai
    check_env("GEMINI_API_KEY")
    client = gai.Client(api_key=os.environ["GEMINI_API_KEY"])

    print(f"[1/5] [Gemini] 上傳音檔: {audio_path.name} ...")
    with open(audio_path, "rb") as f:
        file_obj = client.files.upload(
            file=f,
            config={"mime_type": _guess_mime(audio_path), "display_name": audio_path.name}
        )
    for _ in range(60):
        file_obj = client.files.get(name=file_obj.name)
        if file_obj.state.name == "ACTIVE":
            break
        if file_obj.state.name == "FAILED":
            print("[錯誤] 音檔處理失敗")
            sys.exit(1)
        time.sleep(5)
    else:
        print("[錯誤] 音檔上傳逾時")
        sys.exit(1)
    print("[✓] 音檔上傳完成")

    print("[2/5] [Gemini] 生成逐字稿（含說話者辨識）...")
    response = client.models.generate_content(
        model=model,
        contents=[TRANSCRIPTION_PROMPT, file_obj]
    )
    return response.text.strip()


# ──────────────────────────────────────────────────────────
# Mode B: 本地伺服器轉錄
# ──────────────────────────────────────────────────────────

def transcribe_local(audio_path: Path, num_speakers: int = None) -> str:
    check_env("LOCAL_SERVER_IP", "LOCAL_SERVER_PORT")

    base_url = f"http://{os.environ['LOCAL_SERVER_IP']}:{os.environ['LOCAL_SERVER_PORT']}"
    api_key = os.environ.get("LOCAL_API_KEY", "").strip()
    headers = {"X-API-Key": api_key} if api_key else {}

    # Step 1: 上傳音檔，建立工作
    print(f"[1/5] [本地] 上傳音檔至 {base_url} ...")
    with open(audio_path, "rb") as f:
        data = {"lang": "zh", "device": "cuda"}
        if num_speakers:
            data["num_speakers"] = str(num_speakers)
        resp = requests.post(
            f"{base_url}/transcribe",
            headers=headers,
            files={"audio": (audio_path.name, f, _guess_mime(audio_path))},
            data=data,
            timeout=60
        )
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    print(f"[✓] 工作已建立，Job ID: {job_id}")

    # Step 2: 輪詢狀態（每 15 秒）
    print("[2/5] [本地] 等待轉錄完成（每 15 秒查詢一次）...")
    for attempt in range(120):  # 最多等 30 分鐘
        time.sleep(15)
        r = requests.get(f"{base_url}/jobs/{job_id}", headers=headers, timeout=30)
        r.raise_for_status()
        status = r.json().get("status")
        print(f"  狀態: {status} ({(attempt+1)*15}s)")
        if status == "done":
            break
        if status == "failed":
            error = r.json().get("error", "未知錯誤")
            print(f"[錯誤] 轉錄失敗: {error}")
            sys.exit(1)
    else:
        print("[錯誤] 轉錄逾時（超過 30 分鐘）")
        sys.exit(1)

    # Step 3: 下載逐字稿
    print("[✓] 轉錄完成，下載逐字稿...")
    r = requests.get(f"{base_url}/jobs/{job_id}/result", headers=headers, timeout=60)
    r.raise_for_status()

    # 清理工作（釋放伺服器空間）
    try:
        requests.delete(f"{base_url}/jobs/{job_id}", headers=headers, timeout=10)
    except Exception:
        pass

    return r.text.strip()


# ──────────────────────────────────────────────────────────
# 會議記錄生成
# ──────────────────────────────────────────────────────────

MINUTES_PROMPT = """
你是一位專業的會議記錄整理員。
請根據以下逐字稿，整理成正式的繁體中文會議記錄。

會議記錄須包含以下章節（Markdown 格式，使用 ## 標題）：

## 會議重點摘要
- 條列本次會議討論的主要議題與結論（3-8 條）

## 決議事項清單
列出所有明確決定的事項，每項包含：
- **決議內容**：
- **負責人**：
- **期限**：（若未提及則填「待確認」）

## 待辦追蹤事項

以 Markdown 表格列出所有需要後續跟進的行動項目：

| 追蹤事項 | 負責人 | 期限 |
|---|---|---|
| （內容） | （負責人） | （期限，若未提及填「待確認」） |

## 需要上報的關鍵資訊
- 條列需向上級或相關單位匯報的重要資訊（若無則填「無」）

---
注意：
- 僅保留以上四個章節，其他閒聊或不相干的內容不需記錄
- 英文專有名詞保留英文原文
- 若逐字稿中人名不明確，以 Speaker N 代稱

逐字稿如下：
"""

def generate_minutes(transcript: str, model: str) -> str:
    from google import genai as gai
    check_env("GEMINI_API_KEY")
    client = gai.Client(api_key=os.environ["GEMINI_API_KEY"])

    print("[3/5] 生成會議記錄（Gemini）...")
    response = client.models.generate_content(
        model=model,
        contents=MINUTES_PROMPT + transcript
    )
    return response.text.strip()


# ──────────────────────────────────────────────────────────
# HTML 轉換 & 郵件發送
# ──────────────────────────────────────────────────────────

def minutes_to_html(minutes_md: str, title: str) -> str:
    body_html = md_lib.markdown(minutes_md, extensions=["tables", "fenced_code"])
    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: "Noto Sans TC", Arial, sans-serif; color: #222; max-width: 800px; margin: 0 auto; padding: 24px; }}
  h1 {{ color: #1a1a2e; border-bottom: 2px solid #4a90d9; padding-bottom: 8px; }}
  h2 {{ color: #2c3e50; margin-top: 28px; }}
  h3 {{ color: #34495e; }}
  ul {{ line-height: 1.8; }}
  li {{ margin-bottom: 4px; }}
  p {{ line-height: 1.7; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; }}
  th {{ background: #4a90d9; color: white; }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 24px 0; }}
  .footer {{ font-size: 0.85em; color: #888; margin-top: 32px; }}
</style>
</head>
<body>
<h1>📋 {title} — 會議記錄</h1>
{body_html}
<div class="footer">本會議記錄由 Jarvis 自動生成</div>
</body>
</html>"""


def send_email(recipients: list, subject: str, html_body: str, attachment_path: Path):
    print(f"[5/5] 發送郵件給: {', '.join(recipients)} ...")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    from_name = os.environ.get("EMAIL_FROM_NAME", "Jarvis 會議助理")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{smtp_user}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with open(attachment_path, "rb") as f:
        part = MIMEBase("text", "plain", charset="utf-8")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{attachment_path.name}"')
    part.add_header("Content-Type", f'text/plain; charset="utf-8"; name="{attachment_path.name}"')
    msg.attach(part)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipients, msg.as_bytes())
    print("[✓] 郵件已寄出")


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Meeting transcription + minutes workflow")
    parser.add_argument("audio", help="錄音檔路徑")
    parser.add_argument("--mode", choices=["gemini", "local"], default="gemini",
                        help="轉錄模式：gemini（預設）或 local（本地伺服器）")
    parser.add_argument("--step", choices=["all", "transcribe", "email"], default="all",
                        help="執行步驟：all=完整流程 / transcribe=僅轉錄 / email=僅寄信（讀現有檔案）")
    parser.add_argument("--emails", nargs="+", metavar="EMAIL",
                        help="收件人 email（可省略，讀 .env EMAIL_RECIPIENTS）")
    parser.add_argument("--model", default="gemini-2.5-flash",
                        help="Gemini 模型，僅 gemini 模式使用（default: gemini-2.5-flash）")
    parser.add_argument("--num-speakers", type=int, default=None,
                        help="說話者人數，僅 local 模式使用（不填自動偵測）")
    args = parser.parse_args()

    check_env("SMTP_USER", "SMTP_PASS")

    audio_path = Path(args.audio).resolve()
    if not audio_path.exists():
        print(f"[錯誤] 找不到音檔: {audio_path}")
        sys.exit(1)

    # 收件人
    if args.emails:
        recipients = args.emails
    else:
        env_r = os.environ.get("EMAIL_RECIPIENTS", "")
        recipients = [e.strip() for e in env_r.split(";") if e.strip()]
        if not recipients:
            print("[錯誤] 未指定收件人，請用 --emails 或在 .env 設定 EMAIL_RECIPIENTS")
            sys.exit(1)
        print(f"[✓] 從 .env 讀取收件人: {', '.join(recipients)}")

    # 資料夾
    base_name = re.sub(r"---[0-9a-f\-]{36}$", "", audio_path.stem)
    out_dir = audio_path.parent / base_name
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = out_dir / f"{base_name}_逐字稿.md"
    minutes_path = out_dir / f"{base_name}_會議記錄.md"

    # ── Step: transcribe ──────────────────────────────────
    if args.step in ("all", "transcribe"):
        print(f"[0] 建立資料夾: {out_dir}  [模式: {args.mode.upper()}]")
        if args.mode == "gemini":
            check_env("GEMINI_API_KEY")
            transcript = transcribe_gemini(audio_path, args.model)
        else:
            transcript = transcribe_local(audio_path, args.num_speakers)
        transcript_path.write_text(transcript, encoding="utf-8")
        print(f"[✓] 逐字稿已存: {transcript_path}")

    # ── Step: minutes（僅 gemini 模式 or --step all + gemini）─
    if args.step == "all" and args.mode == "gemini":
        check_env("GEMINI_API_KEY")
        transcript = transcript_path.read_text(encoding="utf-8")
        minutes = generate_minutes(transcript, args.model)
        minutes_full = f"# {base_name} 會議記錄\n\n" + minutes
        minutes_path.write_text(minutes_full, encoding="utf-8")
        print(f"[✓] 會議記錄已存: {minutes_path}")

    # ── Step: transcribe only（本地模式）→ 結束，交由 Agent 生成會議記錄 ──
    if args.step == "transcribe":
        print(f"\n✅ 轉錄完成！請 Agent 讀取逐字稿並生成會議記錄：\n   {transcript_path}")
        print(f"   會議記錄請寫入：{minutes_path}")
        print(f"   完成後執行寄信：python3 {__file__} {audio_path} --step email")
        return

    # ── Step: email ───────────────────────────────────────
    if args.step in ("all", "email"):
        if not minutes_path.exists():
            print(f"[錯誤] 找不到會議記錄檔：{minutes_path}")
            print("請先生成會議記錄再執行寄信步驟。")
            sys.exit(1)
        minutes_full = minutes_path.read_text(encoding="utf-8")
        print("[*] 轉換會議記錄為 HTML ...")
        html_body = minutes_to_html(minutes_full, base_name)
        subject = f"【會議記錄】{base_name}"
        send_email(recipients, subject, html_body, transcript_path)

    print(f"\n✅ 完成！輸出位於: {out_dir}")


if __name__ == "__main__":
    main()
