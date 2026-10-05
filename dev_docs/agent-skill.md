# ZEAL agent skill

## Contract

Keep `line-bot setup`, `line-bot resume`, and `reset` available with their existing
terminal interaction. Add a packaged, cross-host `zeal` skill and an agent entry
point sharing the setup implementation through context-local interaction.

The agent entry point owns one detached setup worker per workspace. Only the
worker runs in the background: the LINE browser is a visible (`headless=False`)
window. Human handoff unlocks the page and brings it to the foreground. A short CLI
call starts the worker, reads JSON status, answers a particular prompt, or requests
cancellation. The worker retains the Playwright context while waiting. It runs
until the next question or final result; there is no second browser controller.

Public status distinguishes running, awaiting input, awaiting human action,
awaiting a local secret, completed, failed, cancelled, and lost worker. Prompt IDs
must be matched when answering. Recovery uses the existing retry and remote-state
checks; creating a new worker is never an automatic retry. Completion requires
Webhook verification and the user's confirmation of a real Bot reply.

Credentials stay in the worker and generated `.env`. Secret prompts use a
loopback-only password form. Agent answer requests cannot supply secrets. Status
contains sanitized messages, supported next actions, verification, and completed
operations, without credential values or raw browser data. Control descriptors
are local, ignored by Git, and must not be included in skill distribution.
Start appends `.zeal-agent/` and the default browser-profile exclusion to the
workspace's `.gitignore` without replacing existing rules. Custom profiles must
also be excluded by their owner. The first version requires a local desktop agent.

Cancelled jobs unwind the shared flow, preserving its verified-service lifetime
rules. Cancellation during a blocking external call takes effect at the next
interaction/checkpoint. Process loss cannot preserve an in-memory browser;
report it without replaying any external action. A user can explicitly abandon
lost local control state before starting a new job, after closing its old browser.

## Delivery and validation

### Existing application mode

Guided setup asks whether to generate a Python Bot or connect an existing
application. `--existing-project PATH` on `line-bot setup` or `agent start` selects
the latter directly; it is mutually exclusive with `--output`. The selected
directory must exist. New-project mode retains the generated Flask workflow.

Existing mode does not generate source files, create a venv, install Bot packages,
start or stop the user's app, or edit its `.env`. ZEAL saves channel credentials
in `.zeal-line/channel-<id>.env` and appends its exclusion to `.gitignore`.
Matching sidecar credentials are reused; changed credentials get another filename,
without overwriting earlier files. Secret values are not sent to the agent.

The worker pauses with `prompt.task=prepare_existing_project` and
`project_context` containing the directory and credential-file path. An existing
LINE-capable app can simply use its original startup command, such as `npm run dev`.
The host agent may help start it using the user's instructions. If it lacks LINE
handlers, explain that gap and ask whether the user wants coding work; ZEAL does
not implement arbitrary framework-specific handlers itself.

The user supplies a full HTTPS Webhook URL (its path and trailing slash are kept)
or a local port plus callback path for a ZEAL/ngrok tunnel. An open local port is
not treated as LINE readiness. LINE API verification, enabled endpoint state, and
the user's phone reply confirmation remain required. Remote deployments do not
need a listening local port. ZEAL owns only tunnels it starts in this mode.

Integration code in an existing application is the coding host's responsibility,
with user authorization. It can reference the sidecar's path and variable names
without reading or transmitting its values. The existing app may keep its own
matching credentials. No source or credential migration is inferred automatically.

Ship one canonical SKILL.md inside the Python package. Install project-local or
global presets for Codex, Claude Code, and Antigravity, plus a custom skills root.
Refuse overwrite unless `--force` is explicit. No host browser tools or MCP server
are required for this first version. Host browser adapters are a later extension.

Validate existing CLI regression tests, prompt freshness, cancellation, human
secret routing, redaction, worker process IPC, cross-host installation, and wheel
resources. Local fixture validation is separate from live LINE/agent E2E.

## Validation: 2026-10-05

- `uv run pytest --basetemp .tmp/pytest-existing-all -q -p no:cacheprovider`:
  149 passed, 1 skipped, 17 subtests passed.
- Existing-application fixtures cover preserving JavaScript source and `.env`,
  separate credentials, custom Webhook paths, deployed endpoints, readiness tasks,
  and stopping only ZEAL-owned tunnels. No framework-specific integration or real
  LINE request was exercised by these fixtures.
- The packaged skill passed skill-creator's frontmatter/resource validation.
- A built wheel installed into an isolated environment supplied both skill files;
  its installed CLI accepted `agent start --existing-project`, started a detached
  worker, published the initial prompt, and cancelled without launching LINE or
  creating a browser profile.
- Local Chromium smoke testing used a visible window, including a launcher with
  the same detached/no-console worker settings on Windows. Automation showed the
  grey gate; human handoff showed the green unlocked message; automation resumed.
  Screenshots were inspected. There were no external requests or real credentials.
- Shared-flow fixtures verify that a successful Webhook alone does not complete
  setup, that the final reply requires user confirmation, and that cancellation
  preserves verified services by default but stops them with the explicit option.
- Project/global/custom installation presets and overwrite preservation were
  tested. Live LINE setup and actual Codex/Claude Code/Antigravity behavioral E2E
  were not performed. The new commands have not been published to PyPI.
