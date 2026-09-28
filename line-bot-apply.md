# LINE Bot：從申請到最小可運作範例

本文件建立一個最小的 LINE Messaging API 回聲機器人：使用者傳文字給官方帳號後，Bot 回覆「你說的是：...」。

執行環境為 Windows PowerShell、Python、Flask 與 ngrok。這是本機開發與測試流程；正式服務再將同一支程式部署到具有效 HTTPS 憑證的主機即可。

> 若使用本專案提供的 ZEAL 工具，可先執行 `uv sync`，再執行 `uv run zeal line-bot setup`。當 LINE 顯示可用 Provider 時，ZEAL 會在終端列出編號讓你選擇既有 Provider 或輸入新 Provider 名稱；不必預先知道或記憶 Provider 名稱。它會建立 `line-bot-<名稱>/`、處理 ngrok、在非人類驗證階段自動操作 LINE Console，並將讀取到的密鑰寫入被 Git 忽略的 `.env`。登入、OTP/MFA、CAPTCHA 與其他人類驗證仍須由帳號本人完成；ZEAL 完成後會重用同一個本機 profile 回到 headless 模式。Provider 不可日後移轉，因此不能由程式猜測。

## 成品與流程

完成後會有下列資料流：

```text
LINE 使用者
  -> LINE Platform
  -> HTTPS Webhook URL（ngrok）
  -> 本機 Flask 程式（localhost:8000/callback）
  -> LINE Messaging API
  -> 使用者收到回覆
```

## 0. 事前準備

需要的帳號與工具如下：

| 項目 | 用途 | 必要性 |
| --- | --- | --- |
| LINE 帳號 | 登入與測試 Bot | 必要 |
| LINE Business ID | 建立 LINE Official Account | 必要 |
| Python 3.10 以上 | 執行範例程式 | 必要 |
| ngrok 帳號與程式 | 提供 LINE 可連線的公開 HTTPS 網址 | 本機測試必要 |
| 程式編輯器（VS Code 等） | 編輯檔案 | 建議 |

本機程式的 `localhost` 無法被 LINE Platform 從網際網路存取；因此測試時必須使用 ngrok、Cloudflare Tunnel 或部署主機其中之一。本文使用 ngrok。

## 1. 安裝 Python 與建立專案環境

### 1.1 安裝 Python

到 [Python 官方下載頁](https://www.python.org/downloads/windows/) 安裝 Python 3.10 以上版本。安裝時勾選 **Add Python to PATH**。

重新開啟 PowerShell，確認安裝成功：

```powershell
py --version
pip --version
```

### 1.2 建立虛擬環境與安裝套件

在想存放專案的位置執行：

```powershell
mkdir line-echo-bot
cd line-echo-bot

py -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install flask requests
```

若 PowerShell 因執行原則拒絕啟用虛擬環境，可只針對目前視窗執行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 2. 安裝並設定 ngrok

1. 到 [ngrok](https://ngrok.com/download) 註冊帳號並下載 Windows 版。
2. 解壓縮 `ngrok.exe` 到自己可管理的位置，例如 `C:\tools\ngrok\`，或依 ngrok 安裝頁的方式安裝。
3. 在 ngrok Dashboard 複製你的 **authtoken**。
4. 在 PowerShell 執行一次：

```powershell
C:\tools\ngrok\ngrok.exe config add-authtoken "貼上你的-ngrok-authtoken"
```

5. 之後啟動本機隧道時執行：

```powershell
C:\tools\ngrok\ngrok.exe http 8000
```

ngrok 會顯示類似下列輸出：

```text
Forwarding  https://abc123.ngrok-free.app -> http://localhost:8000
```

本文件稍後會將 `https://abc123.ngrok-free.app/callback` 填入 LINE 的 Webhook URL。免費方案的網址可能在重新啟動 ngrok 後改變；改變後需更新 LINE Console 中的 URL。

> 若已將 `ngrok` 放到系統 `PATH`，上述指令可簡化為 `ngrok http 8000`。

## 3. 建立 LINE Official Account 與 Messaging API

1. 到 [LINE Official Account Manager](https://manager.line.biz/) 登入。
2. 若尚未有 Business ID，依畫面選擇以 LINE 帳號或 Email 註冊。
3. 建立一個 LINE Official Account，填入名稱、業種等必要資料。
4. 在該 Official Account 的設定中啟用 **Messaging API**。
5. 選擇既有或新增一個 Provider。

Provider 代表提供服務的個人或組織。若未來要搭配 LINE Login、Mini App 或既有公司服務，先確認應歸屬的 Provider；建立後不能將 channel 改移至另一個 Provider。

啟用 Messaging API 後，前往 [LINE Developers Console](https://developers.line.biz/console/)，選擇剛才的 Provider，即可看到已建立的 Messaging API channel。

> Messaging API channel 目前是由 LINE Official Account 啟用 Messaging API 後建立，不是在 Developers Console 直接新增。

## 4. 取得 Bot 的密鑰

在 LINE Developers Console 開啟該 Messaging API channel：

1. 在 **Basic settings** 找到並複製 **Channel secret**。
2. 在 **Messaging API** 分頁的 Channel access token 區塊，發行並複製 **Channel access token**。

兩者的用途不同：

| 值 | 用途 | 是否可公開 |
| --- | --- | --- |
| `Channel secret` | 驗證傳入 webhook 的 LINE 簽章 | 不可 |
| `Channel access token` | 授權程式呼叫 LINE API 回覆訊息 | 不可 |

不要將任何一個值寫進 Git、提交到 GitHub、放進網頁 JavaScript，或傳到公開聊天室。若不慎外洩，應立即在 Console 重新發行相應憑證並更新部署環境。

## 5. 建立最小 Bot 程式

在專案資料夾建立 `app.py`，內容如下：

```python
import os
import json
import hmac
import base64
import hashlib

import requests
from flask import Flask, abort, request

app = Flask(__name__)

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]


def valid_signature(body: bytes, signature: str) -> bool:
    """確認 POST 是 LINE Platform 以本 channel 發出的。"""
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply(reply_token: str, text: str) -> None:
    response = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    )
    response.raise_for_status()


@app.post("/callback")
def callback():
    # 必須以原始 bytes 驗證簽章；不可先 parse 或格式化 JSON。
    raw_body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not valid_signature(raw_body, signature):
        abort(400)

    payload = json.loads(raw_body)

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue

        user_text = event["message"]["text"]
        reply(event["replyToken"], f"你說的是：{user_text}")

    # LINE 用 Verify 測試時會送 events: []；仍應回傳 200。
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

此程式刻意不依賴特定 LINE SDK 版本，但完整做了 webhook 簽章驗證。任何知道公開 webhook URL 的人都可以傳 HTTP 請求，因此不能省略驗證。

## 6. 設定本機密鑰並啟動程式

在已啟用虛擬環境的 PowerShell 輸入。請將引號中的文字替換為自己的實際值：

```powershell
$env:LINE_CHANNEL_SECRET = "你的-Channel-secret"
$env:LINE_CHANNEL_ACCESS_TOKEN = "你的-Channel-access-token"

python app.py
```

看到下列訊息表示本機伺服器正在執行：

```text
Running on http://127.0.0.1:8000
```

上述 `$env:` 設定只對目前這個 PowerShell 視窗有效，適合開發測試。若用 `setx` 寫入永久使用者環境變數，請務必避免把它們印到終端紀錄、文件或版本控制檔案，而且要重新開啟 PowerShell 才會生效。

## 7. 建立公開網址並設定 Webhook

保持 `python app.py` 執行中，開啟第二個 PowerShell 視窗並執行：

```powershell
C:\tools\ngrok\ngrok.exe http 8000
```

複製輸出中的 HTTPS Forwarding 網址，並加上 `/callback`。例如：

```text
https://abc123.ngrok-free.app/callback
```

回到 LINE Developers Console 的 **Messaging API** 分頁：

1. 在 **Webhook URL** 按 **Edit**。
2. 貼上完整的 `https://.../callback` 網址，按 **Update**。
3. 按 **Verify**；成功時會顯示 `Success`。
4. 開啟 **Use webhook**。
5. 在同一頁以 QR Code 將 Official Account 加為好友。

Webhook URL 必須是公開可連線、使用 HTTPS 且有受一般瀏覽器信任的 SSL/TLS 憑證；LINE 不接受自簽憑證。

## 8. 測試

在手機 LINE 中開啟剛加入的官方帳號，傳送：

```text
你好
```

正常情況下會收到：

```text
你說的是：你好
```

若 Official Account Manager 的 Greeting message 或 Auto-reply message 已啟用，建議在測試程式時先關閉，避免平台設定與程式同時回覆而造成混淆。

## 常見問題

### Verify 失敗

依序確認：

1. `python app.py` 是否仍在執行。
2. ngrok 是否仍在執行，且轉送到 `http://localhost:8000`。
3. Webhook URL 是否是 `https://` 開頭，並包含 `/callback`。
4. ngrok 重啟後網址是否已變更。
5. 電腦、公司網路或防毒軟體是否封鎖 Python 或 ngrok。

### Verify 成功，但傳訊息沒有回覆

依序確認：

1. Console 的 **Use webhook** 是否已開啟。
2. `LINE_CHANNEL_SECRET` 與 `LINE_CHANNEL_ACCESS_TOKEN` 是否來自同一個 channel。
3. PowerShell 中是否在設定環境變數的同一個視窗啟動 `python app.py`。
4. `app.py` 主控台是否出現 400、401 或 403 錯誤。
5. Channel access token 是否已失效、被撤銷或貼錯。

### 為什麼不能略過簽章驗證？

LINE 以 `X-Line-Signature` 標頭提供 HMAC-SHA256 簽章。程式會使用 Channel secret 和「未修改的原始 request body」重算簽章；不相符便回傳 400。LINE 不公布固定來源 IP，因此應以簽章驗證，而不是 IP allowlist 判定請求來源。

## 下一步

最小回聲 Bot 運作後，可逐步加入：

- 依關鍵字回覆或串接資料庫
- Flex Message、按鈕與圖文選單（Rich Menu）
- 對話狀態、會員綁定與權限設計
- 部署到 Render、Railway、Azure、AWS、GCP 或自有伺服器
- 使用環境變數管理工具、日誌、例外處理與監控

## 官方文件

- [開始使用 Messaging API](https://developers.line.biz/en/docs/messaging-api/getting-started/)
- [建立 Bot 與設定 Webhook URL](https://developers.line.biz/en/docs/messaging-api/building-bot/)
- [驗證 Webhook 簽章](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)
- [傳送 Reply Message](https://developers.line.biz/en/docs/messaging-api/sending-messages)
- [Webhook URL 驗證](https://developers.line.biz/en/docs/messaging-api/verify-webhook-url/)
