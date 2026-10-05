# Agent CLI contract

All agent commands return UTF-8 JSON. Run them in the workspace used for start.
Shell-quote user input safely for the current shell; never interpolate secrets.

```text
zeal agent start [--output PATH] [--port 8000] [--skip-browser-install]
                 [--browser-profile PATH] [--stop-after-setup]
zeal agent start --existing-project PATH
zeal agent status --session ID [--wait 0..30]
zeal agent answer --session ID --prompt PROMPT_ID [--value TEXT]
zeal agent cancel --session ID
zeal agent abandon --session ID --closed-browser
```

`start` begins the shared guided setup and runs until its next interaction.
`answer` resumes it until the next interaction; it does not blindly repeat a step.
`status` is read-only. `cancel` cooperatively unwinds the worker; a blocking call
may need to finish first. `abandon` releases only unreachable worker control
state and does not stop a browser, service, or external operation. Use it only
after the user has closed the old browser and reviewed uncertain remote changes.

Status fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Currently 1 |
| `session`, `revision` | Job identity and status revision |
| `state` | starting/running/awaiting_input/awaiting_human/awaiting_secret/cancelling/completed/failed/cancelled/worker_lost/abandoned |
| `step` | Current numbered setup phase, or null before account selection |
| `completed_operations` | Successful shared operations; not proof of overall completion |
| `prompt` | Current ID, kind, label, live options, optional local human URL |
| `error` | Error code and, during recovery, the affected operation and retry policy |
| `messages` | Recent sanitized setup messages, including explanations and runtime details |
| `verification` | `webhook_verified` and final user `reply_confirmed` |
| `result` | Generated project path after completion |
| `allowed_actions` | Supported next control actions |
| `project_context` | Optional existing-project mode, directory, and separate credential-file path |
| `prompt.task` | `prepare_existing_project` when the original app must be started/configured before verification |

`--existing-project` and `--output` are mutually exclusive. Without either,
guided setup asks whether ZEAL should generate a Python Bot or connect an
existing app. Existing mode uses `.zeal-line/channel-<id>.env` for separately
saved credentials, keeps the original `.env` and application source, and does
not create/install/start a Python Bot environment. Do not read the sidecar's
secret values. Supply a full HTTPS endpoint or the actual local port plus the
app's callback path. Original app processes are never stopped by ZEAL.

The local password form sends directly to a loopback-only ZEAL server. Secret
values are held in worker memory and saved only by the existing credential/ngrok
flow, never in public job status or answer history. Do not fetch or fill this
form with agent tools. Its URL is a local capability: share only with the user.
Local files live under `.zeal-agent/`, are ignored by Git, and must not be uploaded.
This protects against accidental disclosure, not malicious code running as the
same OS user. There is no network-facing server or second browser automation tool.

For an invalid/stale answer, read status and use the current prompt. For a live
recovery prompt, inspect the offered options; there is no universal safe retry.
If the worker disappears, do not infer whether an external write completed.
Use the saved completed operations as context, not as current remote evidence.

## Install for another agent

```text
zeal install-skill codex
zeal install-skill claude
zeal install-skill antigravity
zeal install-skill codex --global
zeal install-skill --dest /custom/skills
```

Project presets use `.agents/skills/zeal` for Codex/Antigravity and
`.claude/skills/zeal` for Claude. Global presets use `~/.agents/skills/zeal`,
`~/.claude/skills/zeal`, or `~/.gemini/config/skills/zeal` respectively.
For Antigravity CLI with a distinct skills root, use `--dest`.
Existing skill directories require explicit `--force`, which overwrites only the
packaged files, preserving unrelated files. Restart/refresh host discovery if needed.
