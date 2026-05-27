# 小暖 v2 — 家庭生命紀錄系統

LINE Bot，記錄家庭的文字、照片、影片、PDF，分類存入 Google Drive，統計記入 Google Sheets。

---

## 系統架構

```
LINE App
  ↓ Webhook
Render (Flask + gunicorn)
  ├── Google Drive（媒體檔案）
  ├── Google Sheets（文字 + 媒體記錄）
  └── Gemini AI（文字回覆）
```

| 元件 | 說明 |
|------|------|
| 部署平台 | Render Free tier |
| 文字 AI | Gemini 2.5-flash（google-genai SDK） |
| 媒體儲存 | Google Drive（個人 OAuth2） |
| 資料記錄 | Google Sheets（Service Account） |
| 防休眠 | cron-job.org 每 5 分鐘 ping /healthz |

---

## 功能一覽

### 私訊功能
| 操作 | 小暖回應 |
|------|---------|
| 傳文字 | Gemini AI 回覆 + 記錄到 Sheets |
| 傳照片 | Quick Reply 選分類 → 壓縮存 Drive + 記錄 |
| 傳影片 | Quick Reply 選分類 → 串流存 Drive + 記錄 |
| 傳 PDF | Quick Reply 選分類 → 存 Drive + 記錄 |
| `#幫助` | 顯示使用說明 |
| `#查詢` | 最近 10 筆對話紀錄 |
| `#月報` | 當月對話筆數 + 媒體類型 + 分類統計 |

### 群組功能
| 操作 | 小暖回應 |
|------|---------|
| 一般文字 | 回「✅ 已記錄」並寫入 Sheets |
| `#指令` | 同私訊處理 |
| `@小暖 文字` | Gemini AI 回覆 |
| 傳照片 / 影片 / PDF | 同私訊，Quick Reply 選分類後存檔 |

### 分類系統
傳媒體後自動出現 Quick Reply 按鈕：

```
[醫療] [旅遊] [生活] [知識] [財經] [其他]
```

點選後存入對應 Drive 子資料夾，Sheets 記錄分類欄。

---

## Google Sheets 結構

### 對話記錄
| EventID | 時間 | 傳送者 | 訊息 | AI回應 | 狀態 |

### 媒體記錄
| EventID | 時間 | 傳送者 | 類型 | 分類 | Drive連結 | 狀態 |

---

## Render 環境變數

| 變數名稱 | 說明 |
|---------|------|
| `LINE_CHANNEL_SECRET` | LINE Bot Channel Secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot Access Token |
| `SPREADSHEET_ID` | Google Sheets 試算表 ID |
| `DRIVE_FOLDER_ID` | Google Drive 根資料夾 ID |
| `GOOGLE_CREDENTIALS_JSON` | Service Account JSON（Sheets 用） |
| `GEMINI_API_KEY` | Google AI Studio API Key |
| `GOOGLE_CLIENT_ID` | OAuth2 Client ID（Drive 用） |
| `GOOGLE_CLIENT_SECRET` | OAuth2 Client Secret |
| `GOOGLE_REFRESH_TOKEN` | OAuth2 Refresh Token |
| `BOT_MENTION_NAME` | 群組 @提及關鍵字（預設 `@小暖`） |

---

## 健康檢查

```
GET /healthz
→ {"status":"ok","sheets":"ok","drive":"ok","gemini":"ok"}
```

---

## 程式結構

```
xiaonan_v2/
├── app.py                    # Flask 主程式 + Webhook + /healthz
├── Procfile                  # gunicorn --workers 1 --threads 4 --timeout 120
├── requirements.txt
├── handlers/
│   └── message_handler.py   # 訊息處理主邏輯（text/image/video/file）
├── services/
│   ├── drive_service.py     # Google Drive 上傳（OAuth2 + 子資料夾）
│   ├── sheets_service.py    # Google Sheets 讀寫（Service Account）
│   └── gemini_service.py    # Gemini AI 回覆
└── storage/
    └── event_schema.py      # TextEvent / MediaEvent 資料結構
```

---

## 本機取得 OAuth2 Refresh Token（一次性）

```bash
pip install google-auth-oauthlib
python get_refresh_token.py
```

執行後瀏覽器登入 Google，取得三個值填入 Render 環境變數：
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`

---

## 技術備註

- **Dedup**：以 LINE message ID 防止重送重複處理，啟動預熱 500 筆
- **Reply 先行**：所有媒體上傳在背景 ThreadPoolExecutor 執行，不阻塞 LINE 30s timeout
- **影片上傳**：串流下載到 `/tmp` 再 resumable 上傳 Drive，峰值記憶體 ~5MB
- **Drive 子資料夾**：首次建立後快取在 process 記憶體，不重複呼叫 API
- **群組 user_id**：隱私設定關閉時以 `group:xxx` 代替
