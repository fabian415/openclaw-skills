# 部屬指南：WhisperX 語音轉錄 API（Docker Compose）

> 目標環境：有 NVIDIA GPU 的 Linux 遠端機器（Ubuntu 20.04 / 22.04）

---

## 目錄

1. [系統需求](#1-系統需求)
2. [伺服器初始化](#2-伺服器初始化)
3. [安裝 Docker + NVIDIA Container Toolkit](#3-安裝-docker--nvidia-container-toolkit)
4. [準備專案檔案](#4-準備專案檔案)
5. [設定環境變數](#5-設定環境變數)
6. [首次啟動](#6-首次啟動)
7. [確認服務正常](#7-確認服務正常)
8. [API 使用方式](#8-api-使用方式)
9. [聲紋庫 API（Speaker Enrollment）](#9-聲紋庫-apispeaker-enrollment)
10. [Nginx 反向代理 + HTTPS（選用）](#10-nginx-反向代理--https選用)
11. [常用維護指令](#11-常用維護指令)
12. [常見問題排查](#12-常見問題排查)
13. [目錄結構說明](#13-目錄結構說明)

---

## 1. 系統需求

| 項目 | 最低需求 | 建議 |
|------|---------|------|
| OS | Ubuntu 20.04 | Ubuntu 22.04 LTS |
| GPU | NVIDIA 4 GB VRAM | 8 GB+ VRAM（RTX 3070 / A10 / T4）|
| NVIDIA Driver | 525 | 最新穩定版 |
| RAM | 16 GB | 32 GB+ |
| 磁碟 | 30 GB | 60 GB+（模型快取 ~5 GB + 音訊輸出）|
| Docker | 24.x | 最新穩定版 |

> `whisperx large-v3` 模型約需 **6 GB VRAM**。
> CPU-only 模式退回 `medium` 模型，速度較慢（即時率約 2–3x）。

---

## 2. 伺服器初始化

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y curl git vim
```

**確認 GPU 驅動已安裝：**
```bash
nvidia-smi
# 應顯示 GPU 型號與驅動版本
```

若驅動未安裝，依 NVIDIA 官方文件安裝對應版本。

---

## 3. 安裝 Docker + NVIDIA Container Toolkit

### 安裝 Docker Engine

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker   # 或重新登入
docker --version
```

### 安裝 NVIDIA Container Toolkit

```bash
# 新增 NVIDIA apt 倉庫
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# 設定 Docker 使用 NVIDIA runtime
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 驗證
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
# 應看到與 nvidia-smi 相同的 GPU 資訊
```

---

## 4. 準備專案檔案

```bash
# 從 git clone（或用 scp 複製）
git clone <your-repo-url> ~/transcribe-api
cd ~/transcribe-api

# 確認以下檔案存在
ls -1
# Dockerfile
# docker-compose.yml
# .env.example
# scripts/
#   transcribe_diarize.py
#   transcribe_api.py
```

**如果是手動複製，最少需要以下結構：**
```
~/transcribe-api/
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── scripts/
    ├── transcribe_diarize.py
    └── transcribe_api.py
```

---

## 5. 設定環境變數

```bash
cd ~/transcribe-api

# 從範本建立 .env
cp .env.example .env
chmod 600 .env

# 編輯填入真實值
vim .env
```

`.env` 最少需填入以下兩項：

```ini
# 必填：Hugging Face Token（語者分離必要）
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 建議：API 驗證金鑰（不填則無認證）
API_KEY=your-secret-api-key-here
```

**取得 HF_TOKEN：**
1. 前往 https://huggingface.co/settings/tokens → 建立 **Read** 權限的 Token
2. 前往 https://huggingface.co/pyannote/speaker-diarization-community-1 → 點擊 **Agree and access repository**
3. 前往 https://huggingface.co/pyannote/embedding → 點擊 **Agree and access repository**（聲紋比對功能需要）

**產生隨機 API_KEY：**
```bash
openssl rand -hex 32
```

---

## 6. 首次啟動

```bash
cd ~/transcribe-api

# 建置 Docker image（首次約 10–20 分鐘，視網速而定）
docker compose build

# 背景啟動服務
docker compose up -d

# 查看啟動日誌
docker compose logs -f
```

> **首次執行**時，WhisperX 會從 Hugging Face 下載模型（約 3–5 GB），
> 這些模型會被快取到 `hf_cache` volume，後續重啟不需重複下載。

---

## 7. 確認服務正常

```bash
# 健康檢查（不需 API Key）
curl http://localhost:8787/health
```

**預期回應（GPU 就緒）：**
```json
{
  "status": "ok",
  "gpu": true,
  "gpu_name": "NVIDIA GeForce RTX 3090",
  "gpu_memory_gb": 24.0,
  "hf_token_set": true,
  "groq_key_set": true
}
```

**開啟 Swagger UI**（瀏覽器）：
```
http://<server-ip>:8787/docs
```

**開啟 ReDoc**（另一種文件格式）：
```
http://<server-ip>:8787/redoc
```

**下載 OpenAPI JSON spec**：
```
http://<server-ip>:8787/openapi.json
```

---

## 8. API 使用方式

### 認證

所有請求需帶 Header（除 `/health` 外）：
```
X-API-Key: your-secret-api-key-here
```

---

### 步驟一：上傳音訊，啟動轉錄

```bash
curl -X POST http://<server-ip>:8787/transcribe \
  -H "X-API-Key: your-secret-api-key-here" \
  -F "audio=@/path/to/meeting.mp3" \
  -F "lang=zh" \
  -F "device=cuda" \
  -F "num_speakers=3"
```

**回應（HTTP 202）：**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "任務已建立，請使用 GET /jobs/{job_id} 查詢進度。"
}
```

| 參數 | 型別 | 預設 | 說明 |
|------|------|------|------|
| `audio` | file | 必填 | 音訊檔 |
| `lang` | string | `zh` | 語言：`zh` / `en` / `ja` / `auto` |
| `device` | string | `auto` | `auto` / `cpu` / `cuda` / `mps` |
| `num_speakers` | int | null | 語者人數（不填自動偵測）|
| `no_punctuation` | bool | `false` | `true` = 跳過標點補強 |

---

### 步驟二：查詢工作狀態（每 15 秒輪詢）

```bash
curl http://<server-ip>:8787/jobs/550e8400-e29b-41d4-a716-446655440000 \
  -H "X-API-Key: your-secret-api-key-here"
```

**回應（進行中）：**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running",
  "created_at": "2026-03-11T10:00:00Z",
  "updated_at": "2026-03-11T10:00:05Z",
  ...
}
```

**狀態說明：**
- `pending` → 排隊中
- `running` → 轉錄進行中
- `done` → 完成 ✓
- `failed` → 失敗（查看 `error` 欄位）

---

### 步驟三：下載逐字稿

```bash
curl http://<server-ip>:8787/jobs/550e8400-e29b-41d4-a716-446655440000/result \
  -H "X-API-Key: your-secret-api-key-here" \
  -o meeting_transcript.md
```

**逐字稿格式範例：**
```markdown
# 逐字稿 - meeting-2026-03-11

**語言:** zh
**總時長:** 01:00:00
**語者人數:** 3

---

**[00:00:05 → 00:00:32] Speaker 1:**
大家好，今天我們來討論第一季的業績報告。

**[00:00:33 → 00:01:10] Speaker 2:**
好的，根據數據顯示，本季營收成長了 15%。
```

---

### 其他操作

**列出所有工作：**
```bash
# 列出全部
curl http://<server-ip>:8787/jobs \
  -H "X-API-Key: your-secret-api-key-here"

# 只看已完成的
curl "http://<server-ip>:8787/jobs?status=done" \
  -H "X-API-Key: your-secret-api-key-here"
```

**刪除工作（釋放磁碟空間）：**
```bash
curl -X DELETE http://<server-ip>:8787/jobs/550e8400-e29b-41d4-a716-446655440000 \
  -H "X-API-Key: your-secret-api-key-here"
```

---

### Python 輪詢範例（含聲紋比對）

```python
import time
import requests

BASE_URL = "http://<server-ip>:8787"
HEADERS = {"X-API-Key": "your-secret-api-key-here"}

# 1. 上傳音訊
with open("meeting.mp3", "rb") as f:
    resp = requests.post(
        f"{BASE_URL}/transcribe",
        headers=HEADERS,
        files={"audio": f},
        data={"lang": "zh", "device": "cuda"},
    )
resp.raise_for_status()
job_id = resp.json()["job_id"]
print(f"Job ID: {job_id}")

# 2. 輪詢狀態
while True:
    r = requests.get(f"{BASE_URL}/jobs/{job_id}", headers=HEADERS)
    data = r.json()
    print(f"Status: {data['status']}")
    if data["status"] == "done":
        break
    if data["status"] == "failed":
        print(f"Error: {data['error']}")
        exit(1)
    time.sleep(15)

# 3. 下載逐字稿
r = requests.get(f"{BASE_URL}/jobs/{job_id}/result", headers=HEADERS)
with open("transcript.md", "wb") as f:
    f.write(r.content)
print("已儲存 transcript.md")
```

---

## 9. 聲紋庫 API（Speaker Enrollment）

### 概述

聲紋庫功能讓系統能將轉錄結果中的匿名標籤（`Speaker 1`、`Speaker 2`）
自動置換為真實姓名。

**流程：**
```
1. 上傳每位 Speaker 的 10-15 秒錄音 → POST /speakers/enroll
2. 照常呼叫 POST /transcribe 上傳會議錄音
3. 系統自動比對聲紋，逐字稿中直接顯示姓名
```

**技術細節：**
- 使用 `pyannote/embedding`（ECAPA-TDNN）提取 512 維聲紋向量
- 以 cosine similarity 比對，預設閾值 `0.75`（可用 `SPEAKER_SIMILARITY_THRESHOLD` 環境變數調整）
- 未達閾值的語段仍顯示為 `Speaker N`

---

### 步驟一：確認 HF 使用條款

除了 diarization 模型外，還需額外接受 embedding 模型的使用條款：

前往 https://huggingface.co/pyannote/embedding → 點擊 **Agree and access repository**

---

### 步驟二：註冊 Speaker

```bash
# Linux / macOS
curl -X POST http://<server-ip>:8787/speakers/enroll \
  -F "name=Alice" \
  -F "audio=@/path/to/alice_sample.wav"

# Windows CMD
curl -X POST http://<server-ip>:8787/speakers/enroll ^
  -F "name=Alice" ^
  -F "audio=@C:\path\to\alice_sample.wav"
```

**回應（HTTP 201）：**
```json
{
  "name": "Alice",
  "message": "Speaker Alice 註冊成功。"
}
```

| 參數 | 型別 | 必填 | 說明 |
|------|------|------|------|
| `audio` | file | ✓ | 10–15 秒單人錄音（wav / mp3 / m4a 等）|
| `name`  | string | ✓ | Speaker 名稱（英數字、中文、底線、連字號）|
| `device` | string | | `auto`（預設）/ `cpu` / `cuda` |

> **注意**：第一次呼叫時會從 HuggingFace 下載 `pyannote/embedding` 模型（約 300 MB），請耐心等待。

---

### 步驟三：確認已註冊的 Speaker

```bash
curl http://<server-ip>:8787/speakers
```

**回應：**
```json
{
  "total": 2,
  "speakers": [
    {"name": "Alice", "source_file": "alice_sample.wav", "dim": 512},
    {"name": "Bob",   "source_file": "bob_sample.mp3",   "dim": 512}
  ]
}
```

---

### 步驟四：上傳會議錄音（照常操作）

```bash
curl -X POST http://<server-ip>:8787/transcribe \
  -F "audio=@meeting.mp3" \
  -F "lang=zh"
```

聲紋比對在背景自動執行，逐字稿中會直接顯示姓名：

```markdown
**[00:00:05 → 00:00:32] Alice:**
大家好，今天我們來討論第一季的業績報告。

**[00:00:33 → 00:01:10] Bob:**
好的，根據數據顯示，本季營收成長了 15%。

**[00:01:11 → 00:01:45] Speaker 3:**
（未辨識的語者仍顯示為 Speaker N）
```

---

### 刪除 Speaker

```bash
curl -X DELETE http://<server-ip>:8787/speakers/Alice
```

**回應：**
```json
{"message": "Speaker Alice 已從聲紋庫刪除。"}
```

---

### 調整比對閾值

在 `.env` 修改後重啟容器：

```ini
# 調低（如 0.65）：更容易比對到，但可能誤判
# 調高（如 0.85）：更嚴格，僅在非常確定時才置換
SPEAKER_SIMILARITY_THRESHOLD=0.75
```

---

## 10. Nginx 反向代理 + HTTPS（選用）

若需要對外公開服務或使用 HTTPS：

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx

sudo tee /etc/nginx/sites-available/transcribe-api << 'EOF'
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 600M;
    client_body_timeout 300s;

    location / {
        proxy_pass         http://127.0.0.1:8787;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/transcribe-api /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 申請 SSL 憑證（需有網域指向此 IP）
sudo certbot --nginx -d your-domain.com
```

---

## 11. 常用維護指令

```bash
# 查看即時日誌
docker compose logs -f

# 重新啟動服務（更新 .env 後需重啟）
docker compose restart

# 停止服務
docker compose down

# 更新程式碼後重新建置並啟動
docker compose down
docker compose build --no-cache
docker compose up -d

# 進入容器 shell（debug 用）
docker compose exec transcribe-api bash

# 查看容器資源使用（含 GPU）
docker stats transcribe-api
nvidia-smi

# 查看 volume 使用量
docker system df -v
```

---

## 12. 常見問題排查

### GPU 在容器內無法使用

```bash
# 確認 NVIDIA toolkit 設定
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi

# 若失敗，確認 nvidia-container-toolkit 設定
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### HF_TOKEN 錯誤 / 模型下載失敗

```bash
# 進入容器手動測試
docker compose exec transcribe-api python3 -c "
import os
from pyannote.audio import Pipeline
p = Pipeline.from_pretrained(
    'pyannote/speaker-diarization-community-1',
    use_auth_token=os.environ['HF_TOKEN']
)
print('OK')
"
```

常見原因：
1. **未接受使用條款** → 前往 https://huggingface.co/pyannote/speaker-diarization-community-1 點擊 Agree
2. **Token 沒有 Read 權限** → 重新在 HF 產生有 Read 權限的 Token

### 聲紋比對失敗：`name 'AudioDecoder' is not defined`

這是 `pyannote.audio` 某些版本的 circular import bug，`speaker_db.py` 已透過
用 `librosa` 手動載入音訊（傳 `{"waveform": tensor, "sample_rate": 16000}`）來繞過此問題。

若仍遇到此錯誤，請確認容器內的 `speaker_db.py` 是最新版本：

```bash
docker cp scripts/speaker_db.py transcribe-api:/app/speaker_db.py
docker restart transcribe-api
```

### 聲紋比對失敗：`HF_TOKEN 未設定`

`.env` 中的 `HF_TOKEN` 需要同時接受以下兩個模型的使用條款：
- `pyannote/speaker-diarization-community-1`（diarization）
- `pyannote/embedding`（enrollment 聲紋比對）

### 容器啟動後立刻退出

```bash
docker compose logs transcribe-api
# 查看錯誤訊息，常見原因：.env 內的 HF_TOKEN 格式錯誤
```

### VRAM 不足（OOM）

在 `docker-compose.yml` 加入環境變數限制模型大小：
```yaml
environment:
  WHISPER_MODEL: medium    # large-v3（預設）→ medium（較省 VRAM）
```

並在 `scripts/transcribe_diarize.py` 第 213 行對應修改：
```python
model_name = os.environ.get("WHISPER_MODEL", "large-v3") if resolved_device != "cpu" else "medium"
```

### 上傳大檔超時

```bash
# 確認 .env 的設定
MAX_FILE_MB=1000   # 調大限制

# 重啟服務
docker compose restart
```

---

## 13. 目錄結構說明

```
~/transcribe-api/
├── Dockerfile                   # 建置 image 的指令
├── docker-compose.yml           # 服務編排設定
├── .env                         # 環境變數（含機密，不入 git）
├── .env.example                 # 環境變數範本
├── scripts/
│   ├── transcribe_diarize.py    # 核心：WhisperX 轉錄 + 語者分離邏輯
│   ├── transcribe_api.py        # FastAPI 服務主程式
│   └── speaker_db.py            # 聲紋庫：enrollment + 聲紋比對邏輯
└── docs/
    ├── api-spec.yaml            # OpenAPI 3.0 完整規格文件
    └── deploy-transcribe-api.md # 本文件

# Docker Volumes（資料持久化）
volumes/
├── hf_cache         # HuggingFace 模型快取（~5 GB，重啟後不重複下載）
├── uploads          # 音訊暫存（轉錄完成後自動刪除）
├── speaker_profiles # 聲紋庫（{name}.json，每人約 8 KB）
└── outputs/
    ├── _jobs/           # 工作狀態 JSON（job_id.json）
    └── <job_id>/        # 每個工作的輸出
        └── <name>_逐字稿.md
```
