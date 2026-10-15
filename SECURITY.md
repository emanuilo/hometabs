# Security Policy

Hometabs gives a Telegram chat control of a real, logged-in web browser.
Please read this before running it.

## Threat model

**What the bot can do.** The agent drives your Aside Browser through the aside
MCP: it can open pages, read them, click, type and submit forms in a browser
that may be signed in to your email, bank, shops and social accounts. Anyone
who can make the agent act can do the same.

**Who can talk to it.** Only Telegram user IDs listed in `ALLOWED_USER_IDS`.
The bot refuses to start without that list, and every message, command and
button press from other users is rejected and logged. Telegram user IDs can't
be spoofed by other users, but anyone with access to an allowed Telegram
account (an unlocked phone, a hijacked session) can control your browser.

**What the agent is allowed to use.** Every tool of the aside MCP server
(the permission rule `mcp__aside`, so tools added by future Aside versions are
allowed too) and the `Skill` tool for an allowlisted set of skills. It has no
shell, file or web fetch tools, your `~/.claude` settings, CLAUDE.md, plugins
and other MCP servers are not loaded, and anything not pre-approved is denied
without a prompt.

The Aside tools are allowed by design, and they reach beyond the open tabs:

- `repl` runs JavaScript in your Aside Browser. Besides pages it exposes an
  `aside` global with access to Aside's own settings, channels, sessions,
  routines, projects and PDFs, so the agent can read and change those too.
- `memory_search` returns snippets of Aside's memory (Markdown notes about you,
  people, projects, sites and past browsing). Whatever it returns is sent to
  the model provider as part of the conversation and stored in the transcript.
- `exec` hands a task to Aside's own built-in agent, which runs on your Aside
  plan (and billing) with Aside's own permissions and safeguards, not this
  bot's. Its native confirmations can only be answered in the Aside desktop
  app, so a task may sit waiting there.

The residual risk is that a mistaken or manipulated agent can use any of these,
including changing Aside settings or starting Aside agent tasks, and that a
future Aside update can add tools that are allowed automatically. If you don't
want that, narrow `allowed_tools` in `src/hometabs/agent.py` to specific tools
(e.g. `mcp__aside__repl`).

**Prompt injection is the main residual risk.** Web pages the agent reads can
contain text written to manipulate it ("ignore your instructions and send...").
The agent is told to treat page content as data and to ask you in chat before
irreversible actions (purchases, sending messages, posting, submitting forms,
deleting, changing settings, accepting terms) and before autofilling payment
cards. **These safeguards are instructions to the model, not enforced
controls.** A model can be tricked or make mistakes, the repl tool itself
can do anything the browser can, and an `exec` task follows Aside's agent's own
rules rather than these instructions.

**Credentials.** The agent is told to sign in only with your password
manager's autofill and never to type secrets it reads or that you send in chat.
Don't send passwords or codes to the bot: chat messages are stored by Telegram
and in the Claude Code session transcripts on your machine.

## Recommendations

- **Use a dedicated browser profile** for the bot, signed in only to the sites
  you want it to use. Keep banking, primary email and admin accounts out of it
  where you can.
- Keep `ALLOWED_USER_IDS` to your own account(s), and protect that Telegram
  account (passcode, two-step verification).
- **Keep tokens secret.** `TELEGRAM_BOT_TOKEN` lets anyone impersonate the bot;
  `ANTHROPIC_API_KEY` lets anyone use Claude on your Anthropic Console account;
  `CLAUDE_CODE_OAUTH_TOKEN` gives access to your Claude plan. Keep them only in
  `.env` (git-ignored) with restrictive permissions (`chmod 600 .env`), never
  in logs, issues or screenshots. If one leaks, revoke it (BotFather
  `/revoke`; delete the key in the Claude Console; regenerate with
  `claude setup-token`). Consider a dedicated API key with a spend limit.
- The bot removes every credential and provider variable from its own
  environment and passes only the chosen one to the Claude CLI. It logs which
  kind is used, never the value. Don't rely on this as a secret store.
- Watch what the agent does: progress messages show each browser step
  (including memory searches and tasks handed to Aside's agent), and
  `/stop` interrupts a running task.
- The `.state/` directory and Claude Code's transcripts (under
  `~/.claude/projects/`) contain your conversations, page text and
  screenshots. Treat them as sensitive.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them
privately through GitHub's
[security advisories](https://github.com/emanuilo/hometabs/security/advisories/new)
("Report a vulnerability" on the Security tab). Include what you found, how to
reproduce it and its impact. You should get a reply within a few days, and
we'll coordinate a fix and disclosure with you.

Vulnerabilities in Aside, Claude Code / the Claude Agent SDK or Telegram
themselves should be reported to those vendors.

## Supported versions

Only the latest release on `main` receives security fixes.
