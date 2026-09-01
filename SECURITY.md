# Security Policy

## Secret handling

- Never commit API keys, cookies, tokens, account identifiers, or broker credentials.
- Prefer `finresearch run --ask-key`, which reads the DeepSeek key without echoing it.
- `.env` files and all generated `runs/` artifacts are ignored by Git.
- Runtime logs record provider/model names and hashes, never request headers or secrets.
- Longbridge OAuth credentials remain in the external Longbridge CLI credential store and are never copied into project artifacts.
- If a key is pasted into chat, an issue, a terminal command, or a commit, revoke and rotate it.

## Capability boundary

V1.2 can read and merge local evidence, call one JSON-only LLM adapter, write local reports, and explicitly collect read-only Longbridge quote snapshots. The collector resolves the installed binary itself and can execute only the fixed `quote --format json` command with validated symbols. It exposes no arbitrary subprocess arguments, broker SDK, order schema, trading endpoint, browser tool, or autonomous web fetcher.

The collector runs in a temporary directory, separates stdout from stderr, removes unrelated secrets such as `DEEPSEEK_API_KEY` from the child environment, reduces failure diagnostics to a generic classified message, limits output size, and refuses to overwrite an existing output path (including a dangling symlink). Successful-command stderr text is never persisted; the collection manifest records only its source, SHA-256 digest, and byte count.

Research runs likewise require a new output directory. They are assembled in a private staging directory, revalidate the materialized evidence bundle, and publish through an exclusively created destination so pre-existing files or symlinks are never followed.

Evidence merging is a deterministic preprocessing boundary and never invokes an LLM. Every input bundle, record hash, content hash, referenced sidecar path, and raw hash receives a structural and cryptographic-integrity check before publication. This does not authenticate the upstream publisher, timestamp, license, or factual content. Referenced sidecars must be regular files inside their bundle root and are repackaged at fixed content-addressed paths; duplicate Evidence IDs fail closed, while identical raw blobs may be stored once. Fixed limits cover input count, bundle bytes, Evidence count, artifact count, individual artifact size, and total source bytes scanned, and the effective values are recorded in the merge manifest.

Bundle roots and every sidecar path component are pinned with directory descriptors and traversed without following symlinks; staging files are likewise written and rechecked relative to pinned private-directory descriptors. Merge output must not already exist. Staging creation, publication, rollback, and directory synchronization stay bound to one pinned output-parent descriptor. Publication uses the platform's native atomic no-replace primitive and then revalidates the committed tree. Missing `O_NOFOLLOW`, directory-fd support, or native no-replace support fails closed instead of silently reducing these guarantees. This makes `merge-evidence` a macOS/Linux feature today; Windows is rejected with the stable local-output error. Failed staging trees are deliberately not auto-deleted because portable POSIX APIs cannot condition deletion on an already-open inode; a mode-0700 tree containing partial sensitive artifacts may remain for later non-concurrent administrative cleanup. The audit manifest records hash lineage, counts, and limits but does not independently add input absolute paths, original sidecar names, environment variables, command arguments, or artifact content. User-controlled Evidence fields are retained as data. Generated evidence and raw artifacts remain sensitive research material and must not be committed to Git.

## Reporting

Do not open a public issue containing a vulnerability or secret. Contact the repository owner privately and rotate any exposed credential immediately.
