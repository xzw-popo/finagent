# 证据包合并

## 用途

`merge-evidence` 把两个或更多已存在的 EvidenceBundle 做结构与哈希完整性校验，并重打包为一个自包含证据目录。它适合在调用研究 Agent 之前合并 Longbridge 行情、财报、公告和知识库摘录。

这个阶段不是 Agent，也不调用 LLM。它是一个确定性的证据边界：

```text
多个 EvidenceBundle
        ↓
结构 / 记录 / sidecar 完整性校验
        ↓
内容寻址重打包 + 哈希谱系
        ↓
单个自包含 EvidenceBundle
        ↓
validate / run 中的 allowlist 与 PIT 门禁
```

## 命令

```bash
uv run --no-editable finresearch merge-evidence \
  --evidence runs/quotes/evidence.json \
  --evidence data/filings/evidence.json \
  --evidence data/macro/evidence.json \
  --output runs/combined-evidence
```

- `--evidence FILE` 至少出现两次。每个输入必须是 EvidenceBundle JSON 文件。
- `--output DIR` 必须尚不存在；不提供 `--force`。
- 任何重复 `evidence_id` 都被视为语义冲突，即使两条记录完全相同也不会静默去重。
- 合并后的 Evidence 按 `evidence_id` 排序，与输入参数顺序无关。

成功后，命令会列出 Evidence ID，并打印 `minimum_as_of_for_all_evidence`。该时间是要让所有合并证据通过 PIT 门禁所需的最早 `request.as_of`，即所有记录 `(available_at or known_at)` 的最大值。命令不会自动生成或改写研究委托；使用者仍需显式填写 `allowed_evidence_ids` 和 `as_of`。

## 输出契约

```text
combined-evidence/
├── evidence.json
├── merge.json
└── artifacts/                 # 仅在存在 provenance sidecar 时生成
    └── sha256/
        ├── <64 位 raw_sha256>
        └── <64 位 raw_sha256>
```

`evidence.json` 仍是标准 EvidenceBundle，可以被现有 `load_evidence`、`validate` 和 `run` 独立重载。带 provenance 的 Evidence 会把 `raw_artifact_ref` 改写为 `artifacts/sha256/<raw_sha256>`；因为该路径属于记录哈希域，合并器会重算 `record_sha256`。相同 raw hash 只物化一份，但 Evidence 记录不做静默去重。

`merge.json` 用于审计，包含：

- manifest schema 与工具版本；
- 本次实际使用的全部资源上限；
- 合并时间、输入序号、输入包 SHA-256 和记录数；
- 每条记录的输入/ 输出 `record_sha256` 对应；
- sidecar 的路径、SHA-256、字节数和引用它的 Evidence ID；
- 输出 EvidenceBundle 的 SHA-256、数量和 `minimum_as_of_for_all_evidence`；
- `pit_filter_applied: false`，明确表示未在合并时删除未来证据。

manifest 不主动保存输入的本机绝对路径、原始 sidecar 文件名、CLI 参数、环境变量或 sidecar 内容。Evidence 本身的 `evidence_id`、标题等用户控制字段会按证据契约保留，因此仍应把整个包当作敏感研究材料。

重要的信任边界：当地 SHA-256 自洽只能发现重打包过程中的篡改或意外变化，不能证明上游发行方、时间戳、数据授权或内容正确。生产级来源认证仍需可信回执、签名或不可变账本。

## PIT 边界

`merge-evidence` 不接收 `ResearchRequest`，也不使用 `as_of`。它保留所有证据以及原始的 `published_at/known_at/retrieved_at/available_at`。合并之后的标准路径是：

```bash
uv run --no-editable finresearch validate \
  --request requests/research.json \
  --evidence runs/combined-evidence/evidence.json

uv run --no-editable finresearch run \
  --mode mock \
  --request requests/research.json \
  --evidence runs/combined-evidence/evidence.json \
  --output runs/research-result
```

如果研究委托故意设置了更早的 `as_of`，后来才可用的证据仍保留在合并包中，但会在 `validate/run` 的 PIT 门禁处被拒绝。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 合并成功 |
| `2` | 参数错误，例如少于两个输入 |
| `8` | 输出已存在、权限或写盘失败 |
| `9` | 输入缺失，schema/hash/sidecar 校验失败 |
| `10` | 合并语义冲突，例如重复 `evidence_id` |
| `11` | 任一输入、Evidence、artifact 数量或字节资源上限被超过 |

预期内的运行期错误会输出稳定 `code=<name>`，不打印 traceback，也不把本地证据内容回显到错误信息。

当前安全合并实现需要 POSIX `openat`/directory-fd 能力以及 `O_NOFOLLOW`，并在 macOS 使用 `renameatx_np(RENAME_EXCL)`、在 Linux 使用 `renameat2(RENAME_NOREPLACE)` 发布。Windows 当前不支持 `merge-evidence`，会稳定退出 `8`，而不是降级为不安全的路径操作。

默认资源上限为：32 个输入、单包 16 MiB、全部输入包 64 MiB、JSON 深度 128、500,000 个结构 token、10,000 条 Evidence、10,000 个唯一输出 artifact、单 artifact 256 MiB，以及不同来源 sidecar 累计扫描 2 GiB。Python API 可传入更严格的 `MergeLimits`；产物会把实际值写入 `merge.json`。

目录原子发布后，若最后一次父目录 `fsync` 失败，输出内容仍已完整发布，CLI 保持退出 `0` 并在 stderr 警告“崩溃持久性未确认”；Python API 返回 `durability_confirmed=false`。这不是可对同一 `--output` 直接重试的失败。

## 安全限制

- 每个输入都在写盘前独立完成完整性校验。
- EvidenceBundle、Evidence 数量、sidecar 数量、单文件字节数和总字节数均有固定上限。
- sidecar 必须是证据包目录内的普通文件；路径逃逸、绝对路径、目录或 symlink 会失败关闭。
- 合并器只复制 Evidence 正式引用的 sidecar，不复制 `collection.json` 或其他未引用文件。
- 输出先在私有 staging 目录中建立并闭环校验，然后用平台原生 no-replace 原语一次发布并在提交后复验；系统缺少 `O_NOFOLLOW`/directory-fd 等安全原语，或平台、文件系统不支持安全 no-replace 时，均以退出码 `8` 失败关闭，不做不安全降级。
- staging 的创建、发布、回滚和父目录 `fsync` 绑定在同一个已固定的父目录描述符上。为避免并发换入的子项被自动清理误删，失败后不自动删除 staging；可能留下含部分产物的 `0700` 私有目录，应在确认无并发进程后由管理员或 janitor 清理。

合并后的原始 sidecar 仍可能包含供应商数据，整个输出目录应按敏感研究材料管理，不应提交到 Git。
