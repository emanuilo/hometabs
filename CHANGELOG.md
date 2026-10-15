# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Renamed the project to **Hometabs**: distribution and console command
  `hometabs` (`uv run hometabs`, `python -m hometabs`), Python package
  `hometabs`, repository `github.com/emanuilo/hometabs`.
- Environment variables `ASIDE_BOT_MODEL` and `ASIDE_BOT_EFFORT` are now
  `HOMETABS_MODEL` and `HOMETABS_EFFORT` (the old names are no longer read).
  `ASIDE_COMMAND` and `ASIDE_SKILLS_DIR` are unchanged.
- The skills plugin is now named `hometabs`, so skills are
  `hometabs:sign-in` and `hometabs:1password`. Saved conversations started
  under the old names start fresh once, with a note in the chat.
- The LaunchAgent example is now `docs/hometabs.plist.example` (label
  `com.hometabs.bot`, log `~/Library/Logs/hometabs.log`).

- The agent may now use every tool of the aside MCP server (permission rule
  `mcp__aside` instead of `mcp__aside__repl`), including `memory_search`,
  `exec` and any tool future Aside versions add. The system prompt describes
  when to use `repl`, `memory_search` and `exec`, and progress messages
  summarize memory searches and Aside agent tasks. Because the system prompt
  changed, saved conversations start fresh once. See SECURITY.md for what
  this allows.

### Added

- Image input: send photos, screenshots or image files (sent uncompressed) and
  the caption becomes the instruction (without one, the agent describes the
  image and asks what to do). Albums are collected into one request (up to 10
  images). Files over Telegram's 20 MB bot download limit get a clear reply.
  JPEG/PNG/GIF/WebP pass through when small enough; other formats (HEIC via
  `pillow-heif`, BMP, TIFF, ...) are converted to JPEG, and images are scaled
  to at most 1568 px on the long edge and kept to 1 MB each and 6 MB per
  turn (the API resends earlier images on every turn and caps a request at
  32 MB). Images reach the model as base64 image content blocks; the agent can
  see them but can't upload them to websites. Image turns share the text
  queue, `/stop`, `/new`, progress and reply handling. Other media (voice,
  video, stickers, non-image files) get a short "text and images" reply.
  New dependencies: `pillow`, `pillow-heif`. The system prompt changed, so
  saved conversations start fresh once.
- `ANTHROPIC_API_KEY` support, now the recommended way to authenticate.
  Precedence: `ANTHROPIC_API_KEY` > `CLAUDE_CODE_OAUTH_TOKEN` > local Claude
  Code login. Every credential/provider variable is still scrubbed and exactly
  the chosen one is passed to the Claude CLI; the mode (never the value) is
  logged at startup. `/model` discovery sends the API key as `x-api-key`.

## [0.1.0] - 2026-10-03

### Added

- Telegram bot (python-telegram-bot, long polling) in front of a Claude Agent
  SDK agent whose only browser tool is the aside MCP `repl`, driving your Aside
  Browser.
- Mandatory allowlist of Telegram user IDs (`ALLOWED_USER_IDS`); the bot won't
  start without one.
- Claude auth with `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` or the
  local Claude Code login; other credential and provider variables are
  scrubbed from the environment.
- Locked-down agent: no shell or file tools, no user settings, CLAUDE.md,
  plugins or other MCP servers; non-approved tools are denied.
- One persistent conversation per chat, resumed after restarts
  (`.state/sessions.json`); sessions started under different instructions are
  not resumed.
- `/start`, `/new`, `/stop`, `/model` and `/effort` commands. The model list and
  supported effort levels come from Anthropic's `/v1/models` (cached for 24h);
  the choice is saved in `.state/settings.json` and applied without losing the
  conversation.
- Progress messages for each browser step, a typing indicator, a queue for
  messages sent while a task runs, Markdown to Telegram HTML conversion,
  splitting of long replies, and screenshots forwarded as photos.
- Skills: a `sign-in` skill shipped with the bot, plus Aside's builtin
  `1password` skill copied from the local Aside install at startup
  (`ASIDE_SKILLS_DIR`).
- Agent instructions ask for confirmation in chat before irreversible actions.

[Unreleased]: https://github.com/emanuilo/hometabs/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/emanuilo/hometabs/releases/tag/v0.1.0
