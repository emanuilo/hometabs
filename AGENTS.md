# Agent notes

## Code shape

Run `code-shape check` before you finish. Fix what it reports: read the `fix:` line.
Never run `code-shape baseline`, add `# noqa` / `# type: ignore` / `# pylint: disable`,
or edit `[tool.shape]`, the import-linter contracts or the baseline to get it green.
If a rule seems wrong, stop and ask. `code-shape tighten` is fine: it only locks in
improvements. Guide: https://github.com/emanuilo/code-shape-kit/blob/main/docs/agents.md
