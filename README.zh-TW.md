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

設定流程如下：

- **安裝瀏覽器**
  - ZEAL 使用可見的 Playwright Chromium 操作 LINE 後台。若電腦尚未安裝，ZEAL 會先告知並下載；已有安裝時直接使用。
  - 在 Linux 上，首次安裝也會安裝所需的系統套件，可能需要管理員權限。瀏覽器登入狀態預設保存在執行指令目錄的 `.zeal-line-browser-profile/`，方便下次接續。
- **設定公開網址**
  - 看完終端機的流程說明並按 Enter，選擇「使用 ngrok」或「使用已有的公開 HTTPS 網址」。LINE 需要這個網址，才能把訊息送到本機 Bot；Bot 預設使用 `8000` 埠。
  - 自備網址須已轉送到 Bot 連接埠。可輸入基底網址，例如 `https://bot.example.com`，或完整的 `https://bot.example.com/callback`；ZEAL 會補上 `/callback`，並在 Bot 啟動後請 LINE 測試連線。
  - 選擇 ngrok 時，ZEAL 會檢查或下載程式，並沿用同一個 Bot 埠的既有通道。若尚無 Authtoken，終端機會引導你註冊並以隱藏輸入儲存；免費方案的公開網址可能在重新啟動後改變。
- **設定官方帳號**
  - 選擇「建立新的官方帳號」時，在終端機輸入帳號名稱、公司／店鋪名稱、電子郵件和業種；ZEAL 會將資料填入 LINE 的可見瀏覽器。
  - 選擇「接續既有官方帳號」時，登入後從 ZEAL 即時讀取的可管理帳號清單選擇，並按 Enter 確認，不必重填申請資料。
- **準備本機專案**
  - ZEAL 會以帳號名稱建立 `line-bot-<帳號名稱>/`。重新執行時，未修改的專案可直接沿用。
  - 若既有檔案或 `.env` 與所選 Channel 衝突，可選擇覆寫、使用建議的編號新專案名稱，或自行命名；ZEAL 會先備份被覆寫的資料，再接續設定。
- **在瀏覽器完成登入與驗證**
  - 灰色遮罩表示 ZEAL 正在操作，網頁暫不接受手動輸入；綠色提示表示輪到你操作。LINE 要求登入、OTP／MFA 或人類驗證時，請在同一個瀏覽器親自完成。
  - 完成後 ZEAL 會接手並重新鎖定網頁。新建帳號時，ZEAL 會按下申請表的「建立／確定」、確認頁的「完成」，以及資訊使用同意頁的「同意」（若出現）。
- **啟用 Messaging API 並選擇 Provider**
  - 首次啟用時，ZEAL 會在 LINE Official Account Manager 引導你從即時清單選擇或建立 Provider，再接續 LINE Developers Console 的 Channel 設定。Provider 代表這項服務與 Channel 的經營者，連結後無法改掛到其他 Provider，請確認選擇。
  - 若帳號已有 Messaging API Channel，ZEAL 會沿用已綁定的 Provider 與 Channel，不需重新選擇。
- **保存 Channel 憑證**
  - ZEAL 會取得 Bot 所需的 Channel secret 和 access token，寫入本機專案的 `.env`，不在終端機顯示密鑰。若已有 `.env`，會先核對它是否屬於所選 Channel；請勿公開憑證檔。
- **啟動 Bot 並設定 Webhook**
  - ZEAL 會啟動本機 Bot 與所選的公開連線，將 `/callback` 網址設定到 LINE，測試 LINE 能否送達，並確認 Use webhook 已啟用。
  - ZEAL 也會嘗試關閉 LINE 預設自動回覆，避免同一則訊息收到兩次回覆；若未能完成，終端機會提示你到 LINE Manager 手動關閉。加好友歡迎訊息仍可自行設定。
- **測試回覆與管理程序**
  - 用手機掃描瀏覽器顯示的加好友 QR Code，或使用終端機列出的連結，傳送訊息並確認 Bot 回覆；收到回覆後按 Enter 查看設定摘要。
  - 指令結束後，Bot 預設留在背景執行；若使用 ngrok，它也會繼續執行。摘要列出 PID、日誌位置及目前作業系統的查看與停止指令。若用自備網址，請維持轉送；要修改回覆內容，可編輯專案中的 `app.py`。

日後使用 ngrok 時：

- **確認通道：** 各作業系統都可開啟 <http://127.0.0.1:4040>。PowerShell 可用 `(Test-NetConnection 127.0.0.1 -Port 4040).TcpTestSucceeded` 檢查連接埠；macOS／Linux 可用 `curl -fsS http://127.0.0.1:4040/api/tunnels` 列出目前的通道。
- **只停止 ngrok：** 使用設定摘要列出的 ngrok PID；PowerShell 執行 `Stop-Process -Id <ngrok PID>`，macOS／Linux 終端機執行 `kill <ngrok PID>`。
- **重新啟動 ngrok：** 在另一個終端機執行摘要列出的完整指令。若 ngrok 已加入 `PATH`，各作業系統都可用 `ngrok http 8000`，並將 `8000` 換成 Bot 連接埠。若公開網址改變，請在 LINE Developers Console 更新 Webhook URL（加上 `/callback`）、按 Verify，並確認 Use webhook 已啟用。

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

## 重設本機工具與登入狀態

執行 `zeal reset` 會清理瀏覽器和 ngrok 的本機資料，也可單獨指定項目。ZEAL 會先列出實際清理路徑，輸入 `RESET` 才會執行；無人值守時可加上 `--yes`。以下指令可在 PowerShell 與 macOS／Linux 終端機執行。

```text
zeal reset
zeal reset --all
zeal reset --browser
zeal reset --ngrok
zeal reset --browser --ngrok
```

- `zeal reset`、`zeal reset --all` 等同 `zeal reset --browser --ngrok`。
- `--browser` 會移除這套 ZEAL 使用的 Playwright Chromium 元件，以及目前目錄的 LINE 登入資料。若設定時使用自訂瀏覽器資料夾，請加上 `--browser-profile PATH`；舊版 ZEAL 使用的登入資料夾也會一併清理。Playwright 快取可能被其他專案共用，執行前請核對列出的路徑。Linux 安裝的系統相依套件不會移除。
- `--ngrok` 只會刪除 ZEAL 下載到應用程式資料夾的 ngrok 執行檔，並從使用中的 ngrok 設定檔移除 `authtoken` 欄位，保留其他設定；另行安裝在 `PATH` 的 ngrok 不會被移除。若你另外設定 `NGROK_AUTHTOKEN` 環境變數，仍需自行從終端機或系統設定移除。

執行前請先關閉瀏覽器和 ngrok。產生的 `line-bot-<帳號名稱>/` 專案、其中的 `.env` 與 ZEAL 執行日誌會保留。重設後不會自動執行 setup，也不會變更 LINE 官方帳號或 Messaging API Channel。從原始碼目錄執行時，請在指令前加上 `uv run`。

設定完成後，到 [LINE Official Account Manager](https://manager.line.biz/)
修改官方帳號資料與自動回覆；到
[LINE Developers Console](https://developers.line.biz/console/) 修改 Webhook
與 Messaging API 設定。

若要逐步手動了解流程，請閱讀 [line-bot-apply.md](https://github.com/stoday/ZEAL/blob/main/line-bot-apply.md)。
