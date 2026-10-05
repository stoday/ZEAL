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

執行後依序：選擇新建或接續既有官方帳號、在瀏覽器完成 LINE 登入與驗證、用手機傳訊息並確認 Bot 回覆。ZEAL 會在需要公開連線時才詢問網址。

### 設定細節

- **安裝瀏覽器**
  - ZEAL 使用可見的 Playwright Chromium 操作 LINE 後台。若電腦尚未安裝，ZEAL 會先告知並下載；已有安裝時直接使用。
  - 在 Linux 上，首次安裝也會安裝所需的系統套件，可能需要管理員權限。瀏覽器登入狀態預設保存在執行指令目錄的 `.zeal-line-browser-profile/`，方便下次接續。
- **設定官方帳號**
  - 選擇「建立新的官方帳號」時，在終端機輸入帳號名稱、公司／店鋪名稱、電子郵件和業種；ZEAL 會將資料填入 LINE 的可見瀏覽器。
  - 選擇「接續既有官方帳號」時，登入後從 ZEAL 即時讀取的可管理帳號清單選擇，並按 Enter 確認，不必重填申請資料。
- **準備本機專案**
  - 可選擇「由 ZEAL 建立簡易 Python Bot 與環境」或「使用自己的專案，不建置 Python 環境」。沿用時保留原程式與 `.env`，使用原本的啟動／部署方式。
  - 選擇自動建置時，ZEAL 會以帳號名稱建立 `line-bot-<帳號名稱>/`。重新執行時，未修改的產生專案可直接沿用。
  - 若自動建置的專案檔案或 `.env` 與所選 Channel 衝突，可選擇覆寫、使用建議的編號新專案名稱，或自行命名；ZEAL 會先備份被覆寫的資料，再接續設定。
- **在瀏覽器完成登入與驗證**
  - 灰色遮罩表示 ZEAL 正在操作，網頁暫不接受手動輸入；綠色提示表示輪到你操作。LINE 要求登入、OTP／MFA 或人類驗證時，請在同一個瀏覽器親自完成。
  - 完成後 ZEAL 會接手並重新鎖定網頁。新建帳號時，ZEAL 會按下申請表的「建立／確定」、確認頁的「完成」，以及資訊使用同意頁的「同意」（若出現）。
- **啟用 Messaging API 並選擇 Provider**
  - 首次啟用時，ZEAL 會在 LINE Official Account Manager 引導你從即時清單選擇或建立 Provider，再接續 LINE Developers Console 的 Channel 設定。Provider 代表這項服務與 Channel 的經營者，連結後無法改掛到其他 Provider，請確認選擇。
  - 若帳號已有 Messaging API Channel，ZEAL 會沿用已綁定的 Provider 與 Channel，不需重新選擇。
- **保存 Channel 憑證**
  - ZEAL 會取得 Bot 所需的 Channel secret 和 access token，不在終端機顯示密鑰。自動建置的專案使用 `.env`；沿用自己的專案時，另外存到 `.zeal-line/`，保留原本的 `.env`。請勿公開憑證檔。
- **設定公開網址**
  - 以下啟動方式及 `/callback` 預設值適用於自動建置的 Python Bot。自己的專案使用原本的啟動方式及實際 Webhook 路徑，詳見[沿用自己的專案](#沿用自己的專案)。
  - Channel 準備好後，ZEAL 會先沿用同一個 Bot 埠已在運作的公開連線；若沒有，才讓你選擇「使用已有的公開 HTTPS 網址」或「由 ZEAL 建立測試用網址」。LINE 需要這個網址，才能把訊息送到本機 Bot；Bot 預設使用 `8000` 埠。
  - 自備網址須已轉送到 Bot 連接埠。可輸入基底網址，例如 `https://bot.example.com`，或完整的 `https://bot.example.com/callback`；ZEAL 會補上 `/callback`，並在 Bot 啟動後請 LINE 測試連線。
  - 選擇測試用網址時，ZEAL 會檢查或下載 ngrok。若尚無 Authtoken，終端機會引導你註冊並以隱藏輸入儲存；免費方案的公開網址可能在重新啟動後改變。
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

## 沿用自己的專案

設定時選擇「使用自己的專案，不建置 Python 環境」，或直接指定：

```text
zeal line-bot setup --existing-project .
```

從此原始碼目錄執行時，請使用 `uv run zeal`。請以原指令啟動專案，
例如 `npm run dev`，並使用啟動訊息顯示的實際 port。可提供已部署的完整
HTTPS Webhook 網址，或提供本機 port 與 Webhook 路徑，由 ZEAL／ngrok
建立公開連線；支援 `/api/line/webhook` 等自訂路徑。
只有 port 開啟還不夠，原專案必須能接收 LINE Webhook、驗證簽章並回覆訊息。

ZEAL 將 Channel 憑證另存到 `.zeal-line/`，加上 Git 忽略規則，保留原 `.env`。
若原專案已使用同一個 Channel，可沿用現有設定；需要更新時，請使用原專案的
設定方式。ZEAL 會驗證 LINE 連線，再等待你用手機確認收到回覆。
它不產生 Python 程式、不安裝 Bot 套件，也不啟動或停止原 Bot。
若還缺少 LINE 接收／回覆程式，可請 coding agent 依原技術架構加入；
ZEAL skill 會指出這個準備步驟並等待完成。

## 使用 coding agent 操作

原有終端機流程仍可直接使用。在預計建立 Bot 的工作目錄，依使用的 agent
安裝 ZEAL skill，再請 agent 協助建立或接續 LINE Bot：

```text
zeal install-skill codex
zeal install-skill claude
zeal install-skill antigravity
```

選擇符合你的 agent 的指令即可。尚未發佈的原始碼版本，請在指令前加上
`uv run`，例如 `uv run zeal install-skill codex`。
Codex／Antigravity 安裝到 `.agents/skills/zeal/`，Claude Code 安裝到
`.claude/skills/zeal/`。加上 `--global` 可跨專案使用；其他技能根目錄可用
`zeal install-skill --dest <技能目錄>`。既有 skill 資料夾須明確加上
`--force` 才會覆寫套件提供的檔案。

Skill 使用 ZEAL 的可見 LINE 瀏覽器，不需另外配置 agent 瀏覽器工具。
請在能親自操作該瀏覽器的同一台桌面電腦上執行 agent。
只有管理流程的工作程序在背景執行。需要登入、OTP／MFA 或人類驗證時，
ZEAL 會解除網頁操作鎖並將瀏覽器視窗帶到前景；請親自完成，再告知 agent 接續。
若需要密鑰，請開啟 agent 提供的 ZEAL 本機密碼表單直接輸入，不要貼到聊天中。
最後用手機傳送訊息並確認 Bot 回覆。與終端機流程相同，已驗證的 Bot 與
公開連線預設繼續執行。

需要直接協調或診斷時，下列指令會回傳 JSON：

```text
zeal agent start
zeal agent start --existing-project .
zeal agent status --session <id> --wait 5
zeal agent answer --session <id> --prompt <prompt-id> --value <回答>
zeal agent cancel --session <id>
```

請留在相同工作目錄，使用回傳的工作 ID 與當前問題 ID。省略 `--value`
表示按 Enter 確認；密鑰問題只接受本機表單輸入。取消會等待流程安全接續點，
執行中的外部呼叫可能需要先結束。若工作程序失聯，請先關閉舊瀏覽器並檢查
尚未確認的外部變更，再明確使用
`zeal agent abandon --session <id> --closed-browser` 釋放本機控制狀態。
不要為了重試建立帳號而自動重跑整個工作。

本機控制狀態保存在 `.zeal-agent/`；請勿將它或瀏覽器登入資料提交／上傳。
範圍與驗證方式見 [agent 介面契約](dev_docs/agent-skill.md)。支援跨 agent
安裝；LINE 與各 agent 的實際端到端操作驗證，與本機測試分開計算。

## 其他指令說明

```text
# 只完成設定，結束時停止本次啟動的 Bot 與 ngrok（若有使用）
zeal line-bot setup --stop-after-setup

# 進階：已持有 Messaging API Channel 憑證時，手動建立本機 Bot 專案
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
