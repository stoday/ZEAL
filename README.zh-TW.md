# ZEAL

繁體中文 | [English](https://github.com/stoday/ZEAL/blob/main/README.md)

ZEAL(**Zona's Easy Adapter for Lin-bot**)是一個協助使用者快速建立與設定 LINE Bot 的工具。

ZEAL 的開發靈感來自 [Zona](https://github.com/zonawang)。希望能協助想開發 LINE Bot 的人快速完成基本設定，把時間留給真正想做的 Bot 功能。

## 事前準備

- Python 3.11 以上
- [uv](https://docs.astral.sh/uv/)
- 可登入 LINE Business 的 LINE 帳號
- 已轉送到本機 Bot 的公開 HTTPS 網址，或可註冊 [ngrok 帳號](https://dashboard.ngrok.com/signup)並取得 [Authtoken](https://dashboard.ngrok.com/get-started/your-authtoken)

## 安裝

以下指令可在 PowerShell 與 macOS／Linux 終端機執行。

從 PyPI 安裝正式發佈的套件：

```text
uv tool install zeal-builder
```

若要從原始碼執行，請複製專案並安裝相依套件：

```text
git clone https://github.com/stoday/ZEAL.git
cd ZEAL
uv sync
```

## 執行指令

```text
zeal line-bot setup
```

從原始碼目錄執行時，請使用：

```text
uv run zeal line-bot setup
```

缺少 Playwright Chromium 時，ZEAL 會先告知並下載；在 Linux 上也會安裝所需的系統套件，可能需要管理員權限。

ZEAL 會先說明流程。按 Enter 後，選擇使用自備公開 HTTPS 網址或由 ngrok 建立測試用網址。自備網址須轉送到本機 Bot 的連接埠；可輸入基底網址或以 `/callback` 結尾的完整網址，ZEAL 會在 Bot 啟動後請 LINE 測試是否可連線。選擇 ngrok 時，ZEAL 會檢查程式並在缺少時下載；若已有對應連接埠的通道便沿用。缺少 Authtoken 時，ZEAL 會引導註冊並透過隱藏輸入儲存。接著選擇「建立新的官方帳號」或「接續既有官方帳號」。新建時，依終端機指示輸入帳號資料與業種，ZEAL 會在可見瀏覽器填寫表單。接續時，登入 LINE 後從目前可管理的官方帳號清單選擇並確認，不需重填申請資料。
只有首次啟用 Messaging API 時才會選擇 Provider；若選定帳號已有 Channel，ZEAL 會說明 Provider 已綁定，直接沿用既有 Channel。
畫面顯示灰色遮罩時，ZEAL 正在操作，網頁暫不接受手動輸入；顯示綠色提示時才由你操作。
LINE 要求登入、OTP／MFA 或人類驗證時，請在同一個瀏覽器完成；ZEAL 接手後會重新鎖定網頁。
新建帳號時，LINE 表單的「建立／確定」和確認頁的「完成」都由 ZEAL 自動按下。
若 LINE 顯示「同意我們使用您的資訊」，ZEAL 也會按下該頁的「同意」。
其餘支援的設定會由 ZEAL 自動完成。
重新執行同一指令時，ZEAL 會沿用未修改的本機專案。若專案內容或 `.env` 與所選 Channel 不符，ZEAL 會列出現有路徑及可用的編號新專案名稱，讓你當場選擇覆寫、另建建議名稱的新專案，或自行命名；選完就接續設定，不必重做前面的步驟。覆寫前會先將原專案或憑證移到帶編號的備份名稱。
完成後指令會結束，Bot 留在背景；若選擇 ngrok，它也會繼續執行。摘要會顯示程序 PID、日誌位置，以及目前作業系統的查看和停止指令。使用自備網址時，請自行保持該網址轉送至 Bot。

若使用 ngrok，要確認通道是否仍在執行，可開啟本機狀態頁 <http://127.0.0.1:4040>；PowerShell 也可執行 `(Test-NetConnection 127.0.0.1 -Port 4040).TcpTestSucceeded`。若要只停止 ngrok，使用摘要中的 `Stop-Process -Id <ngrok PID>`；重新啟動時，在另一個終端執行摘要列出的完整 ngrok 指令（若已加入 `PATH`，可用 `ngrok http 8000`，將 8000 換成 Bot 的埠）。公開網址若改變，須在 LINE Developers Console 更新 Webhook URL（加上 `/callback`），再按 Verify 並確認 Use webhook 已開啟。

ZEAL 會透過 LINE 公開的 Messaging API 設定並驗證 Webhook，確認 Use webhook 已啟用。最後會在瀏覽器開啟產生的加好友 QR Code，並在終端機列出加好友連結；掃描後傳訊息給 Bot，確認收到回覆。設定 Webhook 後，ZEAL 會關閉
LINE 的預設自動回覆，避免同一則訊息收到兩次回覆；加好友歡迎訊息仍可自行設定。
ZEAL 會建立 `line-bot-<帳號名稱>/`；若要修改 Bot 回覆內容，編輯其中的 `app.py` 即可。
請保持 `.env` 私密。

## 其他指令說明

```text
# 只完成設定，結束時停止本次啟動的 Bot 與 ngrok（若有使用）
zeal line-bot setup --stop-after-setup

# 手動提供既有 Messaging API channel 憑證，建立本機 Bot 專案
zeal line-bot resume

# 查看所有可用選項
zeal line-bot setup --help
zeal line-bot resume --help

# 查看已安裝的 ZEAL 版本
zeal --version
```

從原始碼目錄執行這些指令時，請在指令前加上 `uv run`。

設定完成後，到 [LINE Official Account Manager](https://manager.line.biz/)
修改官方帳號資料與自動回覆；到
[LINE Developers Console](https://developers.line.biz/console/) 修改 Webhook
與 Messaging API 設定。

若要逐步手動了解流程，請閱讀 [line-bot-apply.md](https://github.com/stoday/ZEAL/blob/main/line-bot-apply.md)。
