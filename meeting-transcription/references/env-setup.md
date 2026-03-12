# 環境變數設定說明

## 必填

| 變數名稱 | 說明 |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio 取得的 API Key |
| `SMTP_USER` | 寄件人 Email（如 Gmail 帳號） |
| `SMTP_PASS` | SMTP 密碼或應用程式密碼 |

## 選填

| 變數名稱 | 預設值 | 說明 |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | SMTP 伺服器 |
| `SMTP_PORT` | `587` | SMTP 連接埠（STARTTLS） |
| `EMAIL_FROM_NAME` | `Jarvis 會議助理` | 寄件人顯示名稱 |

## Gmail 設定注意事項

1. 啟用兩步驟驗證
2. 產生「應用程式密碼」（App Password）作為 `SMTP_PASS`
3. 不要使用 Google 帳戶的登入密碼

## 設定方式

### 方法一：`.env` 檔（建議用於本機測試）

在工作區建立 `.env` 檔，然後在腳本中用 `python-dotenv` 載入：
```
GEMINI_API_KEY=AIza...
SMTP_USER=your@gmail.com
SMTP_PASS=xxxx xxxx xxxx xxxx
```

### 方法二：Shell export

```bash
export GEMINI_API_KEY="AIza..."
export SMTP_USER="your@gmail.com"
export SMTP_PASS="xxxx xxxx xxxx xxxx"
```

### 方法三：OpenClaw secrets

透過 `openclaw secrets set GEMINI_API_KEY` 等指令設定，OpenClaw 會自動注入環境變數。

## 本地伺服器設定

| 變數名稱 | 說明 |
|---|---|
| `LOCAL_SERVER_IP` | 本地 Whisper 伺服器 IP（預設 `172.22.12.162`） |
| `LOCAL_SERVER_PORT` | 伺服器 Port（預設 `8787`） |
| `LOCAL_API_KEY` | 伺服器 API 金鑰（**選填**，若伺服器不需驗證可留空或不設定） |

## 依賴套件安裝

```bash
pip install google-genai markdown python-dotenv requests
```

> 需要 Python 3.9+
