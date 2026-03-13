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
# 內容分類 & 筆記生成
# ──────────────────────────────────────────────────────────

CLASSIFICATION_PROMPT = """
你是一位內容分析師。請根據以下逐字稿內容，判斷這份錄音屬於哪一類別：

1. 商務會議 — 公司內部討論、決策、報告、跨部門協作、專案管理等
2. 訪談與使用者研究類 (User Research) — UX 訪談、記者採訪、口述歷史、消費者研究等
3. 知識學習與演講類 — 線上課程、Podcast、技術研討會、TED Talk、講座等
4. 其他 — 無法歸入以上三類（請簡述類型，例如：心理諮詢、法律會談、銷售電話等）

請僅回覆以下格式（不加任何解釋）：
類別編號: <數字>
類別名稱: <名稱>
說明: <一句話描述>

逐字稿如下：
"""

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

USER_RESEARCH_PROMPT = """
你是一位 UX 研究分析師。請根據以下訪談逐字稿，整理成結構化的研究報告（繁體中文）。

報告須包含以下章節（Markdown 格式，使用 ## 標題）：

## 痛點提取 (Pain Points)
條列受訪者在過程中提到的困難、不滿或障礙。

## 需求與期望 (Needs & Desires)
條列受訪者明確表達希望擁有的功能、服務或改善點。

## 情感分析 (Sentiment Analysis)
識別受訪者在談論特定主題時的情緒波動，以表格呈現：

| 主題 | 情緒傾向 | 關鍵描述 |
|---|---|---|
| （主題） | 興奮／挫折／猶豫／中立 | （相關語句） |

## 逐字稿精簡 (Clean Verbatim)
去除贅字（如：那個、然後、呃、嗯...）後的流暢對話紀錄，保留說話者標記與時間戳記。

---
注意：英文專有名詞保留英文原文。若人名不明確，以 Speaker N 代稱。

逐字稿如下：
"""

KNOWLEDGE_PROMPT = """
你是一位知識整理專家。請根據以下演講／課程逐字稿，整理成結構化的學習筆記（繁體中文）。

筆記須包含以下章節（Markdown 格式，使用 ## 標題）：

## 概念解釋 (Concept Definitions)
針對錄音中出現的專業術語或核心概念進行定義，格式：
**術語名稱**：定義說明

## 結構化大綱 (Structured Outline)
將長篇錄音轉化為具備層級的文章架構：

# H1 主題
## H2 子主題
### H3 細節

## 重點金句 (Quotes)
提取具有啟發性或最適合分享的短句（每條加引號，附說話者與時間點）。

## 問答總結 (Q&A Summary)
若有互動環節，以 Q: / A: 格式分別整理觀眾提問與講者回答。
若無互動環節，此節填「無互動環節」。

---
注意：英文專有名詞保留英文原文。

逐字稿如下：
"""

GENERIC_NOTES_PROMPT = """
你是一位專業的內容整理員。這份逐字稿的類型為：{content_type}

請根據此類型，自動判斷最適合的整理結構，輸出繁體中文的結構化筆記。
至少包含以下幾個方面：
- 核心摘要（3-8 條重點）
- 主要內容整理（依最適合此類型的方式分章節呈現）
- 重要資訊或行動項目（若適用）

使用 Markdown 格式（## 標題層級）。
英文專有名詞保留英文原文。

逐字稿如下：
"""

# 分類編號 → (Label, 檔名後綴, Prompt)
_CATEGORY_MAP = {
    "1": ("會議記錄", "_會議記錄", MINUTES_PROMPT),
    "2": ("研究報告", "_研究報告", USER_RESEARCH_PROMPT),
    "3": ("學習筆記", "_學習筆記", KNOWLEDGE_PROMPT),
}


def classify_transcript(transcript: str, model: str) -> dict:
    """分析逐字稿類型，回傳 {'num', 'name', 'desc', 'label', 'suffix', 'prompt'}"""
    from google import genai as gai
    check_env("GEMINI_API_KEY")
    client = gai.Client(api_key=os.environ["GEMINI_API_KEY"])

    print("[3/5] 分析錄音類型...")
    # 取前 3000 字即可判斷類型
    response = client.models.generate_content(
        model=model,
        contents=CLASSIFICATION_PROMPT + transcript[:3000]
    )
    text = response.text.strip()

    result = {"num": "1", "name": "商務會議", "desc": ""}
    for line in text.splitlines():
        if "類別編號:" in line:
            result["num"] = line.split(":", 1)[1].strip()
        elif "類別名稱:" in line:
            result["name"] = line.split(":", 1)[1].strip()
        elif "說明:" in line:
            result["desc"] = line.split(":", 1)[1].strip()

    num = result["num"]
    if num in _CATEGORY_MAP:
        label, suffix, prompt = _CATEGORY_MAP[num]
    else:
        label, suffix = "整理筆記", "_整理筆記"
        prompt = GENERIC_NOTES_PROMPT.format(content_type=result["name"])

    result["label"] = label
    result["suffix"] = suffix
    result["prompt"] = prompt

    print(f"[✓] 類型: {num}. {result['name']} — {result['desc']}  →  產出: {label}")
    return result


def generate_notes(transcript: str, model: str, category: dict) -> str:
    """依分類結果生成對應格式的筆記，回傳 Markdown 字串"""
    from google import genai as gai
    check_env("GEMINI_API_KEY")
    client = gai.Client(api_key=os.environ["GEMINI_API_KEY"])

    label = category.get("label", "會議記錄")
    prompt = category.get("prompt", MINUTES_PROMPT)

    print(f"[4/5] 生成{label}（Gemini）...")
    response = client.models.generate_content(
        model=model,
        contents=prompt + transcript
    )
    return response.text.strip()


# ── 保留舊名稱作為相容別名 ──────────────────────────────────
def generate_minutes(transcript: str, model: str) -> str:
    """相容舊流程：直接以商務會議 prompt 生成會議記錄"""
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
    meta_path = out_dir / f"{base_name}_meta.json"

    # ── 讀取或設定預設 meta（供 email step 使用）─────────────
    def load_meta() -> dict:
        import json
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
        return {"label": "會議記錄", "suffix": "_會議記錄"}

    def save_meta(category: dict):
        import json
        meta_path.write_text(
            json.dumps({"label": category["label"], "suffix": category["suffix"]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

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

    # ── Step: classify + generate notes（僅 gemini all 模式）─
    if args.step == "all" and args.mode == "gemini":
        check_env("GEMINI_API_KEY")
        transcript = transcript_path.read_text(encoding="utf-8")
        category = classify_transcript(transcript, args.model)
        save_meta(category)
        notes = generate_notes(transcript, args.model, category)
        notes_filename = f"{base_name}{category['suffix']}.md"
        notes_path = out_dir / notes_filename
        notes_full = f"# {base_name} {category['label']}\n\n" + notes
        notes_path.write_text(notes_full, encoding="utf-8")
        print(f"[✓] {category['label']}已存: {notes_path}")

    # ── Step: transcribe only（本地模式）→ 結束，交由 Agent 分類並生成筆記 ──
    if args.step == "transcribe":
        print(f"\n✅ 轉錄完成！請 Agent 讀取逐字稿、分類後生成對應筆記：\n   {transcript_path}")
        print(f"   完成後執行寄信：python3 {__file__} {audio_path} --step email")
        return

    # ── Step: email ───────────────────────────────────────
    if args.step in ("all", "email"):
        meta = load_meta()
        label = meta.get("label", "會議記錄")
        suffix = meta.get("suffix", "_會議記錄")
        notes_path = out_dir / f"{base_name}{suffix}.md"
        if not notes_path.exists():
            # Fallback: 嘗試舊版 _會議記錄.md
            fallback = out_dir / f"{base_name}_會議記錄.md"
            if fallback.exists():
                notes_path = fallback
                label = "會議記錄"
            else:
                print(f"[錯誤] 找不到筆記檔：{notes_path}")
                print("請先生成筆記再執行寄信步驟。")
                sys.exit(1)
        notes_full = notes_path.read_text(encoding="utf-8")
        print(f"[*] 轉換 {label} 為 HTML ...")
        html_body = minutes_to_html(notes_full, base_name)
        subject = f"【{label}】{base_name}"
        send_email(recipients, subject, html_body, transcript_path)

    print(f"\n✅ 完成！輸出位於: {out_dir}")


if __name__ == "__main__":
    main()
