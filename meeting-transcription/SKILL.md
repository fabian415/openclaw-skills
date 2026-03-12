---
name: meeting-transcription
description: 會議錄音一鍵轉逐字稿＋會議記錄＋郵件發送工作流程。當使用者提供錄音檔（mp3/m4a/wav/mp4/flac/aac 等）並要求「轉逐字稿」、「做會議記錄」、「寄給主管」等，或要求執行會議記錄工作流程時觸發。支援兩種模式：(1) Gemini 模式—轉錄與會議記錄皆用 Gemini API；(2) 本地模式—轉錄用本地 Whisper 伺服器，會議記錄由 Agent 原生 AI 生成，不依賴外部 API。收到錄音檔後，必須先（1）列出預設收件人並詢問是否調整，（2）詢問轉錄模式，確認後才執行。
---

# Meeting Transcription Skill

## 概覽

支援兩種轉錄模式。收到錄音檔後，**依序確認以下兩點再執行**：

**① 列出目前預設收件人，詢問是否需要調整**
從 `.env` 的 `EMAIL_RECIPIENTS` 讀取後告知使用者，例如：
「目前預設收件人為：fabian415@gmail.com、fabian.chung@advantech.com.tw，請問有需要調整嗎？」
若需調整，以使用者指定的清單為準（本次有效，不修改 .env）。

**② 詢問轉錄模式**
「請問要使用 Gemini 模式還是本地模式進行轉錄？」

確認完畢後執行：生成逐字稿 → 生成會議記錄 → 寄信

> ⚠️ **Gemini 模式**：轉錄＋會議記錄皆用 Gemini API。**本地模式**：轉錄用本地伺服器，會議記錄由 Agent 原生 AI（Claude）生成，不依賴任何外部 API。

---

## 模式說明

| 項目 | Gemini 模式 | 本地模式 |
|---|---|---|
| 轉錄方式 | Google Gemini API | 本地 Whisper 伺服器 |
| 會議記錄生成 | Gemini API | **Agent 原生 AI（Claude）** |
| 速度 | 較快 | 較慢（需等待 GPU） |
| 費用 | 計費 | 免費 |
| 參數 `--mode` | `gemini` | `local` |
| 所需 .env 變數 | `GEMINI_API_KEY` | `LOCAL_SERVER_IP`, `LOCAL_SERVER_PORT` |

---

## 執行指令

**務必加上 `-u` 與 `PYTHONUNBUFFERED=1`，避免輸出卡住無法即時看到進度。**

### Gemini 模式（一鍵完整流程）

```bash
PYTHONUNBUFFERED=1 python3 -u <script> <音檔路徑> --mode gemini
```

### 本地模式（三步驟，Agent 負責生成會議記錄）

**步驟 1：執行本地轉錄**
```bash
PYTHONUNBUFFERED=1 python3 -u <script> <音檔路徑> --mode local --step transcribe
```
轉錄完成後，腳本會輸出逐字稿路徑與會議記錄預期寫入路徑。

**步驟 2：Agent 讀取逐字稿，生成會議記錄**
- 讀取 `xxx_逐字稿.md`
- 依照以下結構生成繁體中文會議記錄：
  - `## 會議重點摘要`（3-8 條重點）
  - `## 決議事項清單`（決議內容、負責人、期限）
  - `## 待辦追蹤事項`（table 格式：追蹤事項 / 負責人 / 期限）
  - `## 需要上報的關鍵資訊`
- 以 `# <檔名> 會議記錄` 為標題，寫入 `xxx_會議記錄.md`

**步驟 3：寄送郵件**
```bash
PYTHONUNBUFFERED=1 python3 -u <script> <音檔路徑> --step email
```

### 其他參數

```bash
--num-speakers N   # 指定說話者人數（本地模式，提升辨識精確度）
--emails a@x.com   # 覆蓋 .env 收件人清單（本次有效）
--model <name>     # 指定 Gemini 模型（預設 gemini-2.5-flash）
```

`<script>` = `/home/advantech/.openclaw/workspace/skills/meeting-transcription/scripts/meeting_workflow.py`

---

## .env 設定

```
# Gemini API
GEMINI_API_KEY=AIza...

# 郵件
SMTP_USER=sender@gmail.com
SMTP_PASS=app-password
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
EMAIL_FROM_NAME=Jarvis 會議助理
EMAIL_RECIPIENTS=a@example.com;b@example.com

# 本地伺服器
LOCAL_SERVER_IP=172.22.12.162
LOCAL_SERVER_PORT=8787
LOCAL_API_KEY=your-secret-api-key-here
```

詳細設定說明見 `references/env-setup.md`。

---

## 輸出結構

```
同音檔目錄/
└── <檔名>/
    ├── <檔名>_逐字稿.md
    └── <檔名>_會議記錄.md
```

---

## 會議記錄章節

| 章節 | 說明 |
|---|---|
| 會議重點摘要 | 3–8 條主要議題與結論 |
| 決議事項清單 | 決議內容 + 負責人 + 期限 |
| 待辦追蹤事項 | **table 格式**（追蹤事項 / 負責人 / 期限） |
| 需要上報的關鍵資訊 | 需向上級匯報的資訊 |

---

## 本地模式 API 流程

1. `POST /transcribe` → 取得 `job_id`（HTTP 202）
2. 每 15 秒輪詢 `GET /jobs/{job_id}` 直到 `status=done`
3. `GET /jobs/{job_id}/result` 下載逐字稿 markdown
4. 自動刪除工作（`DELETE /jobs/{job_id}`）釋放空間

狀態：`pending` → `running` → `done` / `failed`

---

## 常見問題

| 問題 | 解法 |
|---|---|
| 執行後無任何輸出、畫面卡住 | 一律加 `PYTHONUNBUFFERED=1 python3 -u` |
| 本地伺服器連不上（Max retries exceeded） | 確認伺服器是否啟動、`LOCAL_SERVER_IP` / `LOCAL_SERVER_PORT` / VPN |
| 本地模式 CUDA out of memory | GPU 被佔用，稍後再試或改用 Gemini 模式 |
| `LOCAL_API_KEY` 錯誤 | 向伺服器管理員確認，或留空（伺服器不需驗證時） |
| SMTP 認證失敗 | Gmail 需使用應用程式密碼，見 `references/env-setup.md` |
| 說話者辨識不準 | 本地模式可加 `--num-speakers N` |
| Gemini 模型找不到（404） | 確認模型名稱，可用清單：`gemini-2.5-flash`、`gemini-2.5-pro`、`gemini-2.0-flash` |

## 注意事項

- **Telegram 語音訊息 ≠ 音檔**：Telegram 語音訊息會由 OpenClaw 自動轉錄成文字，但沒有對應的 .mp3 檔；若需要走完整工作流程，請確認 inbound 目錄是否有對應的音檔
- **音檔檔名含 UUID**：腳本會自動去除 `---UUID` 後綴，保持資料夾與檔名整潔
- **本地模式的會議記錄由 Agent 原生 AI 生成**：不呼叫任何外部 API，步驟 2 完全由 Agent 自行完成

## 依賴套件

```bash
# 若 pip 不存在，先用以下方式安裝
curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && python3 /tmp/get-pip.py --user

# 安裝所需套件
python3 -m pip install --user google-genai markdown python-dotenv requests
```
