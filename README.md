# ZEAL

[繁體中文](README.zh-TW.md) | English

ZEAL helps you quickly create and configure a LINE Bot.

Inspired by Zora, ZEAL is named **Zora Easy Adapter for Lin-bot**. It aims to
help anyone who wants to build a LINE Bot finish the basic setup quickly, so
they can spend their time developing the Bot itself.

## Before you begin

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)
- A LINE account that can sign in to LINE Business
- An ngrok account and authtoken

## Install

In this project folder, run:

```powershell
uv sync
```

## Run

```powershell
uv run zeal line-bot setup
```

Follow the terminal prompts to enter the required account information. When
LINE opens a browser and asks you to sign in, enter an OTP/MFA code, or confirm
that you are human, complete that step yourself. ZEAL performs the remaining
supported setup automatically.

At the end, scan the QR code, send the Bot a message, and confirm the reply.
ZEAL creates `line-bot-<account-name>/`; edit `app.py` there to change the
Bot's reply. Keep `.env` private.

## Other commands

```powershell
# Keep the local Bot and ngrok running after setup
uv run zeal line-bot setup --keep-running

# Set up a local Bot project for an existing LINE Messaging API channel
uv run zeal line-bot resume

# View all available options
uv run zeal line-bot setup --help
uv run zeal line-bot resume --help
```

After setup, use [LINE Official Account Manager](https://manager.line.biz/) to
edit account details and automatic replies. Use the
[LINE Developers Console](https://developers.line.biz/console/) to edit the
Webhook and Messaging API settings.

For a detailed manual walkthrough in Traditional Chinese, see
[line-bot-apply.md](line-bot-apply.md).
