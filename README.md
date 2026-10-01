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
- An ngrok account and authtoken

## Install

Install the published package from PyPI:

```powershell
uv tool install zeal-builder
```

To work from the source checkout instead:

```powershell
git clone https://github.com/stoday/ZEAL.git
cd ZEAL
uv sync
```

## Run

```powershell
zeal line-bot setup
```

From the source checkout, use:

```powershell
uv run zeal line-bot setup
```

When Playwright Chromium is missing, ZEAL announces and downloads it. On Linux,
it also installs required system packages, which may require administrator privileges.

ZEAL explains the steps first, then asks whether to create a new Official Account
or continue with an existing one. For a new account, enter its details and choose
an industry in the terminal; ZEAL fills the form in the visible LINE browser.
For an existing account, sign in and select one from the live list of accounts you
can manage, then confirm the choice. ZEAL asks you to choose a Provider only
when enabling Messaging API for the first time. If the account already has a
channel, ZEAL explains that its Provider is already linked and continues with it.
A gray veil means ZEAL is working and the page is locked against manual input;
a green notice means you can use the page. Complete sign-in,
OTP/MFA, and human verification in that browser when LINE asks. ZEAL locks
the page again when it resumes. For a new account, it clicks both the
initial Create button and the final Finish button automatically. If LINE shows
the "Agree to our use of your information" page, ZEAL clicks Agree. If
you rerun setup, ZEAL reuses an unchanged local project. If project files or
`.env` conflict with the selected channel, ZEAL shows the existing path and an
available numbered project name. Choose to overwrite, use the suggested new
project name, or enter your own; setup then continues without repeating earlier steps.
Overwritten project files or credentials are moved to a numbered backup first.
When setup finishes, the command exits while the Bot and ngrok keep running.
The summary shows their PIDs, log paths, and commands to inspect or stop them.

ZEAL sets and tests the Webhook URL through LINE's documented Messaging API,
then confirms that Use webhook is enabled. At the end, it opens a generated
add-friend QR code in the visible browser and prints the add-friend link. Scan
the code, send the Bot a message, and confirm the reply.
ZEAL disables LINE's default auto-response when it enables the Webhook, so
incoming messages receive one Bot reply. Greeting messages remain configurable.
ZEAL creates `line-bot-<account-name>/`; edit `app.py` there to change the
Bot's reply. Keep `.env` private.

## Other commands

```powershell
# Stop the Bot and ngrok when setup finishes instead of leaving them running
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
