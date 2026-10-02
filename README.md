# ZEAL

[繁體中文](https://github.com/stoday/ZEAL/blob/main/README.zh-TW.md) | English

ZEAL (**Zona's Easy Adapter for Lin-bot**) helps you quickly create and
configure a LINE Bot.

Inspired by [Zona](https://github.com/zonawang), ZEAL aims to help anyone who
wants to build a LINE Bot finish the basic setup quickly, so they can spend
their time developing the Bot itself.

## Before you begin

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)
- A LINE account that can sign in to LINE Business
- A public HTTPS URL already forwarding to the local Bot, or an [ngrok account](https://dashboard.ngrok.com/signup) with an [Authtoken](https://dashboard.ngrok.com/get-started/your-authtoken)

## Install

The commands below work in PowerShell and macOS/Linux terminals.

Install the published package from PyPI:

```text
uv tool install zeal-builder
```

To work from the source checkout instead:

```text
git clone https://github.com/stoday/ZEAL.git
cd ZEAL
uv sync
```

## Run

```text
zeal line-bot setup
```

From the source checkout, use:

```text
uv run zeal line-bot setup
```

Setup proceeds as follows:

- **Install the browser**
  - ZEAL uses a visible Playwright Chromium browser for LINE setup. If it is missing, ZEAL announces and downloads it; otherwise it uses the installed copy.
  - On Linux, the first installation also installs required system packages and may need administrator privileges. By default, sign-in state is saved in `.zeal-line-browser-profile/` beside the directory where you run the command, so later runs can continue the session.
- **Set the public URL**
  - Read the terminal introduction, press Enter, and choose ngrok or an existing public HTTPS URL. LINE needs this URL to send messages to the local Bot, which uses port `8000` by default.
  - Your own URL must already forward to the Bot port. Enter a base URL such as `https://bot.example.com` or the full `https://bot.example.com/callback`; ZEAL uses `/callback` and asks LINE to test the connection after starting the Bot.
  - With ngrok, ZEAL checks or downloads the program and reuses an existing tunnel for the same Bot port. If you have no Authtoken, the terminal guides you through registration and saves it through hidden input. A free-plan URL may change after a restart.
- **Set up the Official Account**
  - To create one, enter its name, company or store name, email, and industry in the terminal. ZEAL fills the form in the visible LINE browser.
  - To continue an existing account, sign in, select it from the live list of accounts you can manage, and press Enter to confirm. You do not need to enter the application details again.
- **Prepare the local project**
  - ZEAL creates `line-bot-<account-name>/` using the account name. An unchanged project can be reused on later runs.
  - If existing files or `.env` conflict with the selected channel, choose to overwrite, use a suggested numbered project name, or enter your own. ZEAL backs up the replaced data before continuing.
- **Complete sign-in and verification in the browser**
  - A gray veil means ZEAL is operating and manual input is locked; a green notice means you can use the page. Complete LINE sign-in, OTP/MFA, and human verification yourself in that browser when prompted.
  - ZEAL then resumes and locks the page again. For a new account, it clicks Create, Finish, and Agree on the information-use consent page if shown.
- **Enable Messaging API and choose a Provider**
  - On first activation, ZEAL guides you through choosing or creating a Provider from LINE's live list in Official Account Manager, then continues the channel setup in Developers Console. The Provider owns the service and Channel; the link cannot be moved to another Provider, so confirm your choice.
  - If the account already has a Messaging API channel, ZEAL keeps its linked Provider and channel. There is no new Provider choice.
- **Save the Channel credentials**
  - ZEAL retrieves the Channel secret and access token the Bot needs and saves them in the local project's `.env` without printing the secrets. If `.env` already exists, ZEAL first checks that it belongs to the selected channel. Keep this file private.
- **Start the Bot and configure the Webhook**
  - ZEAL starts the local Bot and the selected public connection, sets the `/callback` URL in LINE, tests that LINE can reach it, and confirms Use webhook is enabled.
  - It also tries to turn off LINE's default auto-response to avoid duplicate replies. If that step does not succeed, the terminal tells you how to turn it off in LINE Manager. Greeting messages remain configurable.
- **Test the reply and manage the processes**
  - Scan the add-friend QR code shown in the browser, or use the link printed in the terminal. Send a message, confirm the Bot replies, then press Enter to see the setup summary.
  - The Bot remains running in the background by default, along with ngrok if selected. The summary lists PIDs, log paths, and commands for your operating system to inspect or stop them. Keep forwarding active if you use your own URL; edit `app.py` in the generated project to change the reply.

If you use ngrok later:

- **Check the tunnel:** Open <http://127.0.0.1:4040> on any OS. PowerShell can check whether the inspector port is listening with `(Test-NetConnection 127.0.0.1 -Port 4040).TcpTestSucceeded`; macOS/Linux can list the active tunnels with `curl -fsS http://127.0.0.1:4040/api/tunnels`.
- **Stop only ngrok:** Use the ngrok PID from the setup summary: `Stop-Process -Id <ngrok PID>` in PowerShell, or `kill <ngrok PID>` in a macOS/Linux terminal.
- **Restart ngrok:** In a new terminal, run the full ngrok command shown in the summary. If ngrok is on `PATH`, use `ngrok http 8000` on any OS, replacing `8000` with the Bot port. If the public URL changes, update the LINE Developers Console Webhook URL with `/callback`, click Verify, and confirm Use webhook is enabled.

## Other commands

```text
# Stop the Bot and ngrok (if used) when setup finishes
zeal line-bot setup --stop-after-setup

# Manually provide credentials for an existing Messaging API channel
zeal line-bot resume

# View all available options
zeal line-bot setup --help
zeal line-bot resume --help

# Show the installed ZEAL version
zeal --version
```

When working from the source checkout, prefix these commands with `uv run`.

After setup, use [LINE Official Account Manager](https://manager.line.biz/) to
edit account details and automatic replies. Use the
[LINE Developers Console](https://developers.line.biz/console/) to edit the
Webhook and Messaging API settings.

For a detailed manual walkthrough in Traditional Chinese, see
[line-bot-apply.md](https://github.com/stoday/ZEAL/blob/main/line-bot-apply.md).
