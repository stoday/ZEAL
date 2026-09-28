# ZEAL

繁體中文 | [English](README.md)

ZEAL(**Zona's Easy Adapter for Lin-bot**)是一個協助使用者快速建立與設定 LINE Bot 的工具。

ZEAL 的開發靈感來自 [Zona](https://github.com/zonawang)。希望能協助想開發 LINE Bot 的人快速完成基本設定，把時間留給真正想做的 Bot 功能。

## 事前準備

- Python 3.11 以上
- [uv](https://docs.astral.sh/uv/)
- 可登入 LINE Business 的 LINE 帳號
- ngrok 帳號與 authtoken

## 安裝

複製專案後安裝相依套件：

```powershell
git clone https://github.com/stoday/ZEAL.git
cd ZEAL
uv sync
```

## 執行指令

```powershell
uv run zeal line-bot setup
```

依終端機指示輸入需要的帳號資訊即可。LINE 開啟瀏覽器並要求登入、輸入 OTP／MFA，
或確認你是人類時，請自行完成該步驟；其餘支援的設定會由 ZEAL 自動完成。

最後掃描 QR Code、傳訊息給 Bot，確認收到回覆。ZEAL 會建立
`line-bot-<帳號名稱>/`；若要修改 Bot 回覆內容，編輯其中的 `app.py` 即可。
請保持 `.env` 私密。

## 其他指令說明

```powershell
# 設定完成後，持續執行本機 Bot 與 ngrok
uv run zeal line-bot setup --keep-running

# 已經有 LINE Messaging API channel 時，建立本機 Bot 專案並完成設定
uv run zeal line-bot resume

# 查看所有可用選項
uv run zeal line-bot setup --help
uv run zeal line-bot resume --help
```

設定完成後，到 [LINE Official Account Manager](https://manager.line.biz/)
修改官方帳號資料與自動回覆；到
[LINE Developers Console](https://developers.line.biz/console/) 修改 Webhook
與 Messaging API 設定。

若要逐步手動了解流程，請閱讀 [line-bot-apply.md](line-bot-apply.md)。
