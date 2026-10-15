# Hometabs

[![CI](https://github.com/emanuilo/hometabs/actions/workflows/ci.yml/badge.svg)](https://github.com/emanuilo/hometabs/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Claude Agent SDK](https://img.shields.io/badge/Claude-Agent%20SDK-blueviolet.svg)](https://pypi.org/project/claude-agent-sdk/)

**Your personal agent in Telegram, using your real browser and logins.**

Hometabs is a personal AI agent you talk to in Telegram. It works in the
browser on your Mac at home, with your logins, cookies, bookmarks and password
manager, so it can do things on any website you can use yourself, including
the ones no app or integration covers: your local bank, the utility company,
a government portal. Ask it from your phone to check an order, look something
up in an account or fill in a form; it shows its progress step by step, sends
screenshots, and asks before doing anything irreversible.

- **Your real browser.** Works with [Aside Browser](https://asidehq.com): the
  agent uses your tabs and sessions instead of a fresh cloud browser.
- **Any website.** If you can do it in your browser, you can ask for it.
- **Lives in Telegram.** No new app to install on your phone.
- **Runs on your own Mac.** Open source; nothing in between except Telegram
  and the Claude API.

Under the hood, Hometabs hands your messages to a
[Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) agent, which
drives the Aside Browser through Aside's MCP server.

## Disclaimer

Hometabs is an unofficial, community project. It is **not affiliated with,
endorsed by or supported by Anthropic, Aside or Telegram**. Aside, Claude and
Telegram are trademarks of their respective owners.

The agent acts in **your real browser**, which may be signed in to your
accounts. AI agents make mistakes and can be manipulated by content on the
pages they read. **Use it at your own risk** and read [SECURITY.md](SECURITY.md)
first.

## Features

- **Real browser control.** The agent can use every tool the Aside MCP server
  exposes, now and in future Aside versions: `repl` (Playwright-style
  JavaScript in your Aside Browser, so it sees your tabs, cookies and logins),
  `memory_search` (Aside's notes about you, your accounts and sites) and
  `exec` (hands a task to Aside's own agent).
- **Allowlist.** Only the Telegram user IDs you list can use it; it won't start
  without the list.
- **Locked down.** No shell or file tools, no `~/.claude` settings, CLAUDE.md,
  plugins or other MCP servers: just the aside MCP tools and an allowlisted set
  of skills.
- **Asks first.** Instructed to confirm in chat before purchases, sending
  messages, posting, submitting forms, deleting, changing settings or accepting
  terms.
- **Password manager sign-in.** Can sign in with the browser's 1Password
  autofill when a task needs it; never types secrets from chat.
- **Send images.** Photos, screenshots and image files (PNG, JPEG, WebP, GIF,
  HEIC, ...) go to the agent with your caption as the instruction; an album
  becomes one request. See [Sending images](#sending-images).
- **Persistent conversations.** One conversation per chat, resumed after
  restarts.
- **Live progress.** A typing indicator, a status line with the current browser
  step, `/stop` to interrupt, and a queue for messages sent mid-task.
- **Model and effort switching.** `/model` and `/effort` with the model list
  discovered from Anthropic's API, so new models show up on their own.
- **Phone-friendly replies.** Markdown converted to Telegram HTML, long replies
  split, screenshots forwarded as photos.
- **One credential, nothing else.** Uses `ANTHROPIC_API_KEY` (recommended),
  `CLAUDE_CODE_OAUTH_TOKEN` or your local Claude Code login; every other
  credential and provider variable is removed from the agent's environment.

## Requirements

- **macOS** with the [Aside Browser](https://asidehq.com) and its CLI
  installed (`aside mcp` must work).
- **Claude access**: an [Anthropic API key](https://platform.claude.com/)
  (recommended), or a Claude Code login (see [Authentication](#authentication)).
  The Agent SDK bundles the Claude Code CLI it runs.
- A **Telegram bot** token from [@BotFather](https://t.me/BotFather).
- [uv](https://docs.astral.sh/uv/) (installs Python 3.10+ for you).
- Optional: the 1Password browser extension in Aside, for sign-ins.

## Where to run it

The agent works in the Aside Browser on the machine where Hometabs runs, so
that Mac needs to be on, awake and logged in for the bot to answer.

- **Best:** an always-on Mac, such as a Mac mini on your desk or a spare
  MacBook kept plugged in. Your agent is then reachable from Telegram any
  time, wherever you are.
- **Fine for trying it out:** your everyday laptop. The bot answers while the
  laptop is awake; when it sleeps, the bot goes quiet. Telegram holds on to
  messages you send in the meantime, and the bot works through them when the
  laptop wakes up, so keep that in mind before sending something like "pay
  this bill" to a sleeping Mac. Messages sent while Hometabs itself wasn't
  running are different: they are skipped at startup, on purpose, so a stale
  request doesn't fire hours later.

Tip: to keep a dedicated Mac awake, open System Settings → **Energy** and turn
on "Prevent automatic sleeping when the display is off" (on a MacBook:
**Battery** → Options → "Prevent automatic sleeping on power adapter when the
display is off").

## Quickstart

1. **Get the code**

   ```sh
   git clone https://github.com/emanuilo/hometabs.git
   cd hometabs
   uv sync
   cp .env.example .env
   ```

2. **Claude credential**: create an API key in the
   [Claude Console](https://platform.claude.com/) and put it in
   `ANTHROPIC_API_KEY`. See [Authentication](#authentication) for the
   alternatives.

3. **Telegram bot**: message [@BotFather](https://t.me/BotFather), send
   `/newbot`, and put the token in `TELEGRAM_BOT_TOKEN`.

4. **Your Telegram user ID**: ask [@userinfobot](https://t.me/userinfobot), or
   start the bot and message it: rejected users are logged with their ID and
   told it in a private chat. Put it in `ALLOWED_USER_IDS`.

5. **Run**

   ```sh
   uv run hometabs
   # or: uv run python -m hometabs
   ```

   Then send your bot something like *"Open example.com and tell me the
   heading"*.

## Authentication

Hometabs picks exactly one Claude credential, in this order:

| Order | Setting | Mode logged at startup |
|---|---|---|
| 1 | `ANTHROPIC_API_KEY` | `api_key` |
| 2 | `CLAUDE_CODE_OAUTH_TOKEN` | `oauth_token` |
| 3 | neither: your local Claude Code login | `local_login` |

- **API key (recommended).** Create one in the
  [Claude Console](https://platform.claude.com/); usage is billed to that
  Console account. Anthropic asks developers building with the Agent SDK to
  use API key authentication. A dedicated key with a spend limit is a good
  idea.
- **Subscription token (optional, personal use).** `claude setup-token`
  prints a long-lived token for your Claude plan; put it in
  `CLAUDE_CODE_OAUTH_TOKEN`. Anthropic restricts how Claude.ai plan
  credentials may be used outside its own apps, so read
  [Anthropic's authentication and credential use policy](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)
  and check the terms for your plan before you use this.
- **Local login.** With neither set, the Claude Code CLI uses whatever login
  it finds on the machine (`claude`, then `/login`). The same terms apply.

If both are set, the API key is used and the startup log says so. Every other
credential or provider variable (`ANTHROPIC_AUTH_TOKEN`,
`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
`CLAUDE_CODE_USE_FOUNDRY`, and whichever of the two above wasn't chosen) is
removed before the Claude CLI starts. Credential values are never logged.

## Configuration

Settings are read from the environment or `.env` (see
[`.env.example`](.env.example)).

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | | Bot token from @BotFather |
| `ALLOWED_USER_IDS` | yes | | Comma-separated Telegram user IDs allowed to use the bot |
| `ANTHROPIC_API_KEY` | no | | Anthropic API key (recommended; takes precedence) |
| `CLAUDE_CODE_OAUTH_TOKEN` | no | local Claude Code login | Token from `claude setup-token` (see [Authentication](#authentication)) |
| `ASIDE_COMMAND` | no | `aside` on `PATH`, else `~/.local/bin/aside` | Aside CLI, started as `aside mcp` |
| `ASIDE_SKILLS_DIR` | no | `~/.aside/u/0/skills/builtin` | Where Aside's builtin skills are copied from |
| `HOMETABS_MODEL` | no | `claude-sonnet-5-5` | Default model (overridden by `/model`) |
| `HOMETABS_EFFORT` | no | `medium` | Default effort: `low`, `medium`, `high`, `xhigh`, `max` (overridden by `/effort`) |
| `STATE_FILE` | no | `.state/sessions.json` | Session store; its directory is the state dir |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

The state dir (`.state/` by default) holds:

| File | Contents |
|---|---|
| `sessions.json` | Chat ID → Claude session ID, plus a fingerprint of the agent's instructions |
| `settings.json` | The model and effort chosen with `/model` and `/effort` |
| `models_cache.json` | The model list from `/v1/models`, cached for 24h |
| `plugin/` | The skills plugin, rebuilt at every start |

The conversations themselves are stored by Claude Code as session transcripts
under `~/.claude/projects/`.

## Commands

| Command | Effect |
|---|---|
| `/start` | Help (also `/help`) |
| `/new` | Forget the conversation and start fresh |
| `/stop` | Interrupt the running task and drop any queued messages |
| `/model` | Show the current model with buttons to switch; `/model <id>` switches directly |
| `/effort` | Show the reasoning effort with buttons for the levels the model supports; `/effort <level>` sets it |

Messages sent while a task runs are queued and handled in order (one agent turn
per chat at a time). Messages sent while the bot wasn't running are dropped on
startup, so stale requests don't run in your browser.

### Sending images

Send a photo, a screenshot or an image file (sent "as file", uncompressed) and
the caption is the instruction, e.g. a screenshot of an error with *"what does
this mean?"* or a photo of a bill with *"pay this on the utility's site"*.
Without a caption, the agent describes the image and asks what to do with it.

- **Albums** (several photos sent together) become one request with all the
  images and the album's caption.
- Up to 10 images per request. Telegram lets bots download files up to
  20 MB; larger files get a reply saying so.
- JPEG, PNG, GIF and WebP are sent as they are when small enough; other
  formats (HEIC, BMP, TIFF, ...) are converted to JPEG, and large images are
  scaled down to 1568 px on the long edge (Claude's own working size) and kept
  under Claude's per-image size limit.
- Images are queued like text messages, and `/stop` and `/new` drop them too.
- The agent **sees** the images but can't **upload** them to websites: they
  are passed to the model, not saved as files the browser could attach.
- Voice messages, videos, stickers and other files get a short reply that
  only text and images are supported.

Images are sent to Anthropic along with the conversation and are stored in the
Claude Code session transcripts. See [SECURITY.md](SECURITY.md).

### Model and effort

The model list isn't hardcoded: `/model` fetches it from Anthropic's
`GET /v1/models` with your credential (the API key as `x-api-key`, or the
OAuth token as a bearer token; sent only to api.anthropic.com). With the local
Claude Code login there is no credential to send, so no list is fetched. Each
model's supported effort levels come from the same response, limited to the
levels the installed Agent SDK accepts. If the list can't be fetched, the last
cached list is used; with no list at all, `/model <id>` still accepts any
`claude-...` ID.

The choice is bot-wide and persisted. A change applies from the next message
and keeps the conversation: the chat's agent reconnects, resuming the same
session with the new model and effort. A task that is already running finishes
with the old setting. If the new model doesn't support the chosen effort, the
nearest lower supported level is used (or effort is omitted for models without
effort support), and the bot tells you.

## How it works

```mermaid
flowchart LR
    phone["Telegram app<br/>(your phone)"] <--> tg["Telegram Bot API"]
    subgraph mac["Your Mac"]
        bot["bot.py<br/>allowlist, per-chat queue,<br/>progress, formatting"]
        agent["agent.py<br/>BrowsingAgent<br/>(ClaudeSDKClient per chat)"]
        state[(".state/<br/>sessions, settings,<br/>model cache, plugin")]
        cli["Claude Code CLI<br/>(bundled with the SDK)"]
        mcp["aside mcp<br/>(repl, memory_search,<br/>exec, ...)"]
        browser["Aside Browser<br/>your tabs and logins"]
        bot --> agent
        bot <--> state
        agent -->|spawns| cli
        cli -->|stdio| mcp
        mcp --> browser
    end
    tg <-->|long polling| bot
    cli <-->|"HTTPS, API key or token"| anthropic["Anthropic API"]
```

When you send a message:

1. The bot long-polls Telegram (no open ports), checks the sender against the
   allowlist and takes the chat's lock; if a task is running, the message is
   queued.
2. The chat's `BrowsingAgent` is created on first use: it spawns the Claude
   Code CLI with a fixed system prompt, the aside MCP server, the skills plugin
   and an environment holding only the chosen Claude credential, resuming the
   chat's saved session if there is one.
3. Claude works in a loop, calling the aside MCP tools: mostly `repl` to read
   and act on pages, `memory_search` when Aside's notes about you would help,
   and `exec` when handing the whole task to Aside's own agent is clearly
   better. An `exec` task runs on your Aside plan and may wait for a
   confirmation that can only be answered in the Aside desktop app; the agent
   is told to say so. Each tool call updates the "Working… step n" status message (at most
   every 2 seconds).
4. The final reply is converted to Telegram HTML, split if needed and sent
   with up to five screenshots. The session ID is saved so the conversation
   survives restarts.

Sessions started under different instructions (system prompt or skills) are
not resumed, because Claude Code keeps a session's original system prompt;
you get a note that a fresh conversation started.

See [docs/architecture.html](docs/architecture.html) for detailed diagrams of
the architecture, the message flow and how history is kept.

## Skills

The agent loads skills only from a local Claude Code plugin named `hometabs`
that the bot builds at startup in `.state/plugin/` (passed to the CLI with
`--plugin-dir`). Your `~/.claude` settings, CLAUDE.md and personal skills and
plugins are never loaded, and only the plugin's skills are allowlisted.

- **`hometabs:sign-in`** (shipped with this project, MIT): tells the agent to
  sign in with the browser's password manager autofill whenever a task needs a
  login, and how to click 1Password's autofill menu reliably.
- **Aside's builtin skills** listed in `ASIDE_SKILLS` in
  [`src/hometabs/skills.py`](src/hometabs/skills.py) (currently `1password`,
  loaded as `hometabs:1password`) are copied unmodified from your local Aside
  install (`ASIDE_SKILLS_DIR`). They belong to Aside and are not distributed
  with this project. If one isn't found, the bot logs a warning and runs
  without it.

Because the plugin is rebuilt on every start, an Aside update is picked up on
the next restart (and starts fresh conversations, since the instructions
changed). To use another Aside skill, add its directory name to
`ASIDE_SKILLS`. The startup log lists the skills that were loaded.

## Security

The bot gives a Telegram chat control of a logged-in browser. Prompt
injection from web pages is the main residual risk, and the "ask before
irreversible actions" rule is an instruction to the model, not an enforced
control. Use a dedicated browser profile, keep the allowlist tight and keep
your tokens and keys secret. See [SECURITY.md](SECURITY.md) for the threat
model and how to report vulnerabilities.

## Running as a service

To keep the bot running in the background and start it at login, use a launchd
LaunchAgent (it must run in your login session, where the Aside Browser runs).
[`docs/hometabs.plist.example`](docs/hometabs.plist.example) is a template:

```sh
# Edit the paths in the template first, then:
cp docs/hometabs.plist.example ~/Library/LaunchAgents/com.hometabs.bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hometabs.bot.plist

# Restart after an update / stop it:
launchctl kickstart -k gui/$(id -u)/com.hometabs.bot
launchctl bootout gui/$(id -u)/com.hometabs.bot
```

Logs go to `~/Library/Logs/hometabs.log`. Run only one instance per bot token:
Telegram allows a single long-polling client.

## Development

```sh
uv sync
uv run pytest               # tests (no network, keys, Aside or Telegram needed)
uv run ruff check           # lint
uv run ruff format          # format
```

```
src/hometabs/
  __init__.py    # entry point: logging, config, env scrubbing, polling
  agent.py       # agent core (no Telegram): ClaudeSDKClient + aside MCP
  auth.py        # credential choice and the scrubbed env for the Claude CLI
  bot.py         # Telegram handlers, per-chat locks, progress, /model, /effort
  config.py      # .env loading and validation
  formatting.py  # message splitting, Markdown -> Telegram HTML
  images.py      # incoming images: size limits, conversion, downscaling
  models.py      # /v1/models discovery (model list, effort levels, cache)
  skills.py      # builds the skills plugin (our skills + Aside's)
  plugin/        # plugin template: manifest + skills we ship
tests/           # pytest suite
docs/            # architecture diagrams, launchd example
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[MIT](LICENSE). Aside's skills loaded at runtime are not part of this project
and remain under Aside's terms.
