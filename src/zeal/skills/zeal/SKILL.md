---
name: zeal
description: Create or continue a LINE Official Account and build a working LINE Bot with ZEAL. Use for LINE Bot onboarding, ZEAL setup, and diagnosing an active ZEAL setup job; not for unrelated browser tasks or deleting local state.
---

# ZEAL LINE Bot

Use ZEAL's persistent setup worker to configure a Bot. ZEAL controls its visible
browser window, protects pages from accidental input, stores credentials locally, and
verifies LINE's Webhook state. You coordinate the user choices and recovery.
Browser tools from the host agent are not required.
Only the workflow worker runs in the background. Keep ZEAL's LINE browser visible;
human handoff unlocks it and brings the window to the foreground.

## Start and continue

Run on the user's local desktop with a terminal tool. A remote/cloud-only session
cannot provide this local browser and password-form handoff to the user.

1. Work in the user's chosen project directory. Check `zeal --version` and
   `zeal agent --help`. In a ZEAL source checkout, use `uv run zeal` instead of
   `zeal` for every command. If ZEAL is missing, install `zeal-builder` with
   `uv tool install zeal-builder`; use the user's requested source/version if given.
2. Run `zeal agent start`. Retain the returned session ID and keep all later
   commands in the same directory. If it reports an existing session, inspect
   that session instead of starting another controller.
   If the user wants their existing application, pass `--existing-project <path>`
   or choose the existing-project option when asked. Do not create a Python Bot
   environment in that mode.
3. Read JSON status. For `running` or `cancelling`, use
   `zeal agent status --session <id> --wait 5`. Do not hold a tool call open
   indefinitely or poll rapidly. Explain meaningful progress to the user.
4. For `awaiting_input`, use the prompt's `label`, `options`, recent `messages`,
   and `error` to understand the question. Ask the user for missing information.
   Reuse answers and authorization already supplied. Provider is the service
   owner and its Channel binding cannot be moved; never guess it or the account.
   Answer with `zeal agent answer --session <id> --prompt <prompt-id> --value <text>`.
   For an Enter confirmation, omit `--value`. Use only current offered options;
   old prompt IDs and double submissions are rejected.
5. For `awaiting_human`, explain the browser task and let the user perform login,
   OTP/MFA, CAPTCHA, or other human verification in ZEAL's browser. Resume only
   after the user explicitly says that action is finished. They may also open
   `prompt.human_url` and continue locally. For the final phone test, require the
   user's explicit confirmation that they received a Bot reply before answering.
6. For `awaiting_secret`, show the `prompt.human_url` link and ask the user to
   enter the value directly in that local password form. Never ask for tokens or
   Channel secrets in chat or submit them through an agent tool. The worker will
   resume after the local form is submitted; read status again. Do not expect to
   receive the secret itself. Poll `status --wait 5` while the worker is running.
   If a prompt remains, compare its ID with the previous prompt before asking for
   another input. Repeat a secret request only when fresh status shows a pending
   secret prompt; explain any sanitized validation error. A page showing `{}` or
   a source-validation failure means submission failed, not that the agent should
   retrieve the token.
7. On completion, report the generated or connected project, verified Webhook, user-confirmed
   reply, and whether services remain running. `running`, a created project, or
   a successful click is not completion. Default services stay in the background.

## Existing project

When `prompt.task` is `prepare_existing_project`, read `project_context` for the
project and separate credential-file path. If the app already handles LINE
Webhooks, use its existing startup/deployment instructions (for example
`npm run dev`) and identify the actual port and callback path. An open port alone
does not prove it can accept LINE messages. Answer the readiness prompt only when
the app is running with the selected Channel's credentials and a LINE handler.

Keep the original `.env`, application files, framework, and dependency workflow.
The app can retain its existing matching credentials; the separate `.zeal-line`
file is available for user-controlled configuration and its values must not be
read through agent tools. If handlers or configuration are missing, explain the
gap and ask whether the user wants you to implement that integration. Only with
that coding scope established, work within the existing application while ZEAL
waits; do not scaffold a replacement Python project. User-entered secrets can be
loaded by the application from the separate file without inspecting the values.

For connection, use the complete deployed HTTPS endpoint or the app's actual
local port and callback path. ZEAL verifies the result and controls only tunnels
it starts, not the original app process.

## Recover

Read [agent-cli.md](references/agent-cli.md) when diagnosing errors, cancelling,
installing for another host, or recovering a lost worker.

Use only the recovery choices in the current prompt. An uncertain account
submission must be inspected before retrying. Never restart an entire job as an
automatic recovery, select a destructive backup/reset choice without the user's
authorization, or modify ZEAL's implementation merely to bypass a failure.
For a transient failure, automatically choose an offered retry at most twice;
if the same failure persists, use human handoff or discuss the cause with the user.
Check non-sensitive configuration and service health when relevant; do not read,
print, copy, export, or transmit `.env`, browser profiles, control descriptors,
cookies, credential-bearing request data, or unsanitized logs. Do not use host
browser tools to compete with ZEAL for the same browser session.

On `failed`, report the error and completed operations without declaring success.
On `worker_lost`, stop automatic execution and explain that in-memory browser
state cannot be resumed. Let the user close the old ZEAL browser and inspect any
uncertain external change before explicitly abandoning it or starting another job.
Cancel only when requested: `zeal agent cancel --session <id>`, then read status
until cancelled. Cancellation preserves already verified services by default.
