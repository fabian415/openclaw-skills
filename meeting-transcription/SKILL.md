---
name: meeting-transcription
description: 會議錄音一鍵轉逐字稿＋自動分類筆記＋郵件發送工作流程。當使用者提供錄音檔（mp3/m4a/wav/mp4/flac/aac 等）並要求「轉逐字稿」、「做會議記錄」、「寄給主管」等，或要求執行會議記錄工作流程時觸發。支援三種模式：(1) Gemini 模式—轉錄、分類與筆記生成皆用 Gemini API；(2) 本地模式—轉錄用本地 Whisper 伺服器，分類與筆記由 Agent 原生 AI 生成；(3) Azure OpenAI 模式—轉錄用 Azure OpenAI gpt-4o-transcribe，分類與筆記由 Agent 原生 AI 生成。自動依內容分為：1.商務會議、2.訪談與使用者研究、3.知識學習與演講、4.其他，並產出對應格式的筆記。收到錄音檔後，必須先（1）列出預設收件人並詢問是否調整，（2）詢問轉錄模式，確認後才執行。
---

# Meeting Transcription Skill

## 概覽

支援**兩種**轉錄模式。收到錄音檔後，**依序確認以下三點再執行**：

> ⚠️ **重要：即使 OpenClaw 的媒體理解功能已自動轉錄音檔（[Audio] block 或 Transcript 出現在訊息中），Agent 仍必須先完成以下三個確認，絕對不得跳過或直接使用自動轉錄的內容。**

**① 告知使用者收到了哪個音檔**
明確說明收到的音檔檔名與路徑，例如：
「我收到了音檔：`會議錄音_20260313.m4a`（路徑：`/path/to/file`），正在準備處理。」

**② 列出目前預設收件人，詢問是否需要調整**
從 `.env` 的 `EMAIL_RECIPIENTS` 讀取後告知使用者，例如：
「目前預設收件人為：fabian415@gmail.com、fabian.chung@advantech.com.tw，請問有需要調整嗎？」
若需調整，以使用者指定的清單為準（本次有效，不修改 .env）。

**③ 詢問轉錄模式**
「請問要使用哪種模式？
1. **Gemini 模式** — 轉錄＋會議記錄皆由 Gemini API 完成（快速）
2. **本地模式** — 轉錄用本地 Whisper GPU 伺服器，筆記由 Claude 生成（免費、離線）
3. **Azure OpenAI 模式** — 轉錄用 Azure gpt-4o-transcribe-diarize（含說話者辨識），筆記由 Claude 生成」

確認完畢後執行：生成逐字稿 → 生成會議記錄 → 固定歸檔 → 詢問是否同步更新專案知識庫 → 寄信

> ⚠️ **Gemini 模式**：轉錄＋會議記錄皆用 Gemini API。**本地模式**：轉錄用本地伺服器，會議記錄由 Agent 原生 AI（Claude）生成，不依賴任何外部 API。**Azure OpenAI 模式**：轉錄用 Azure OpenAI `gpt-4o-transcribe-diarize`，會議記錄由 Agent 原生 AI（Claude）生成。注意：目前 `/audio/transcriptions` 端點僅回傳純文字，無時間戳或說話者標籤。

---

## 模式說明

| 項目 | Gemini 模式 | 本地模式 | Azure OpenAI 模式 |
|---|---|---|---|
| 轉錄方式 | Google Gemini API | 本地 Whisper GPU 伺服器 | Azure OpenAI gpt-4o-transcribe |
| 會議記錄生成 | Gemini API | **Agent（Claude）** | **Agent（Claude）** |
| 速度 | 快 | 慢（GPU 處理） | 快 |
| 費用 | Gemini 計費 | 免費（本地） | Azure 計費 |
| 步驟 | 一鍵完成 | 三步驟 | 三步驟 |
| 所需 .env 變數 | `GEMINI_API_KEY` | `LOCAL_SERVER_IP`, `LOCAL_SERVER_PORT` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_API_KEY` |
| 離線可用 | ❌ | ✅ | ❌ |
| 說話者辨識 | ✅ | ✅ | ⚠️ API 目前回傳純文字（無時間戳） |

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
轉錄完成後，腳本會存檔 `xxx_逐字稿.md` 並提示 Agent 繼續執行步驟 2。

**步驟 2：Agent 分類逐字稿 → 生成對應筆記**
- 讀取 `xxx_逐字稿.md`
- **判斷錄音類型**（商務會議 / 訪談 / 演講 / 其他）並告知使用者
- 依類型生成對應格式筆記（詳見「筆記章節對照」）
- 另外將固定歸檔檔案寫入：
  - `meeting-archives/[日期]_[會議名稱]_逐字稿.txt`
  - `meeting-archives/[日期]_[會議名稱]_會議內容.txt`
- 完成後必須主動詢問使用者：**是否要透過 `project-insight-synthesizer` skill，持續更新專案進度文件？**

### Azure OpenAI 模式（三步驟，Agent 負責生成會議記錄）

**步驟 1：執行 Azure OpenAI 轉錄**
```bash
PYTHONUNBUFFERED=1 python3 -u <script> <音檔路徑> --mode openai --step transcribe
```
轉錄完成後，腳本會輸出逐字稿路徑。使用 `gpt-4o-transcribe-diarize`，支援說話者辨識（SPEAKER_00 → Speaker 1 自動對應）。⚠️ 音檔需小於 25 MB（Azure 限制）。

**步驟 2：Agent 讀取逐字稿 → 分類 → 生成對應格式筆記**
- 讀取 `xxx_逐字稿.md`
- **先判斷錄音類型**（根據逐字稿內容）：
  - **1. 商務會議**：公司內部討論、決策、報告、專案管理等
  - **2. 訪談與使用者研究類 (User Research)**：UX 訪談、記者採訪、口述歷史等
  - **3. 知識學習與演講類**：線上課程、Podcast、技術研討會、演講等
  - **4. 其他**：自行識別類型，靈活調整格式
- 告知使用者識別結果，再依類型生成筆記

**類型 1 — 商務會議** → 寫入 `xxx_會議記錄.md`，依以下固定 template 輸出（全繁體中文，英文專有名詞保留英文原文）：

```markdown
# 會議記錄

> 本份會議記錄由 **Claude Sonnet 4.6**（anthropic/claude-sonnet-4-6）根據逐字稿原文生成。
>（Gemini 模式改標注 Gemini 模型名稱；Azure 模式亦標注 Claude）

## 會議名稱
[公司名稱] 主題說明

## 時間
- 開始時間：YYYY 年 MM 月 DD 日 HH:MM（GMT+8，台北）
- 結束時間：YYYY 年 MM 月 DD 日 ~HH:MM（GMT+8，台北）

## 與會者
姓名1, 姓名2, 姓名3

## 決議事項
1. 決議一
2. 決議二
（視實際決議數量條列）

## 行動事項
| 行動項目 | 負責人 | 期限 |
|---------|--------|------|
| 項目說明 | 姓名   | 日期或「待確認後回覆」 |

⚠️ **負責人必須從逐字稿中出現的實際說話者中選出，不可填「開發團隊」等泛稱**

## 摘要與重點結論
- 3–8 條重點，直接從逐字稿萃取，忠實反映原文

## 詳細討論內容

### 1. 會議目的與背景
- 說明本次會議聚焦主題
- 條列本次會議目標

### 2. 主題一（依討論內容自訂小節標題）
#### 2.1 子主題（視需要分層）
- 條列討論重點

（依實際討論內容，自行新增第 3、4… 節）
```

**類型 2 — 訪談與使用者研究類** → 寫入 `xxx_研究報告.md`（標題：`# xxx 研究報告`）
  - `## 痛點提取 (Pain Points)`：受訪者提到的困難、不滿或障礙
  - `## 需求與期望 (Needs & Desires)`：明確表達的功能或服務期望
  - `## 情感分析 (Sentiment Analysis)`：table 格式（主題 / 情緒傾向 / 關鍵描述）
  - `## 逐字稿精簡 (Clean Verbatim)`：去除贅字後的流暢對話，保留說話者標記

**類型 3 — 知識學習與演講類** → 寫入 `xxx_學習筆記.md`（標題：`# xxx 學習筆記`）
  - `## 概念解釋 (Concept Definitions)`：術語或核心概念定義
  - `## 結構化大綱 (Structured Outline)`：H1/H2/H3 層級架構
  - `## 重點金句 (Quotes)`：具啟發性的短句（附說話者與時間點）
  - `## 問答總結 (Q&A Summary)`：Q: / A: 格式（若無互動則填「無互動環節」）

**類型 4 — 其他** → 寫入 `xxx_整理筆記.md`（標題：`# xxx 整理筆記`）
  - 根據內容類型自行設計最合適的章節結構，至少包含核心摘要與主要內容整理

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

### Azure OpenAI 相關環境變數

```
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o-transcribe
AZURE_OPENAI_API_VERSION=2025-03-01-preview
AZURE_OPENAI_API_KEY=your-azure-api-key
```

---

## 輸出結構

```
同音檔目錄/
└── <檔名>/
    ├── <檔名>_逐字稿.md
    ├── <檔名>_會議記錄.md   ← 類型 1（商務會議）
    ├── <檔名>_研究報告.md   ← 類型 2（User Research）
    ├── <檔名>_學習筆記.md   ← 類型 3（演講／課程）
    ├── <檔名>_整理筆記.md   ← 類型 4（其他）
    └── <檔名>_meta.json     ← 記錄類型與檔名後綴

固定歸檔目錄/
└── /home/advantech/.openclaw/workspace/meeting-archives/
    ├── [日期]_[會議名稱]_逐字稿.txt
    └── [日期]_[會議名稱]_會議內容.txt
```

> 每次只會產出一個筆記檔，依分類結果決定；但固定歸檔路徑一定要同步寫入 `.txt` 檔，方便使用者日後追溯。

---

## 筆記章節對照

| 類型 | 輸出章節 |
|---|---|
| 1. 商務會議 | AI模型註記、會議名稱、時間、與會者（列表）、詳細討論內容（分節）、決議事項（條列）、行動事項（table，負責人從說話者選出）、摘要與重點結論 |
| 2. 訪談與使用者研究 | 痛點提取、需求與期望、情感分析（table）、逐字稿精簡 |
| 3. 知識學習與演講 | 概念解釋、結構化大綱（H1/H2/H3）、重點金句、問答總結 |
| 4. 其他 | 依內容類型自動設計章節 |

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

## 後續互動要求
- 每次轉錄與會議內容整理完成後，Agent 必須主動告知：
  - 固定歸檔的逐字稿路徑
  - 固定歸檔的會議內容路徑
- 然後詢問使用者：**是否要透過 `project-insight-synthesizer` skill，持續更新專案進度文件？**
- **禁止自動直接串接。** 必須等使用者明確回覆「要」、「好」、「請更新」或其他等效同意後，才可繼續更新 `project-insights/` 知識庫與 reviewer。
- 若使用者尚未明確同意，流程到詢問為止，不得自行往下執行。

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
