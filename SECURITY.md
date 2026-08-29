# Security Policy

## Secret handling

- Never commit API keys, cookies, tokens, account identifiers, or broker credentials.
- Prefer `finresearch run --ask-key`, which reads the DeepSeek key without echoing it.
- `.env` files and all generated `runs/` artifacts are ignored by Git.
- Runtime logs record provider/model names and hashes, never request headers or secrets.
- If a key is pasted into chat, an issue, a terminal command, or a commit, revoke and rotate it.

## Capability boundary

V1 can read local evidence, call one JSON-only LLM adapter, and write local reports. It contains no broker SDK, order schema, trading endpoint, shell tool, browser tool, or autonomous web fetcher.

## Reporting

Do not open a public issue containing a vulnerability or secret. Contact the repository owner privately and rotate any exposed credential immediately.
