# M15 · 本地收据适配器

> 🌐 [English](M15-local-receipt-adapter.md) · **中文**

> **状态（2026-10-24）**：⏸️ M15 开发中，作者已决定暂缓。实现与本指南保存在 `wip/m15-agent-accounting-paused`，不在 `main`；以下内容供以后恢复时参考，现在不执行部署。部分收据链路已走查，完整验收未完成；进度及作者跳过的项目见 [M15 恢复检查点](M15_zh.md)。

## 为什么需要此路径

2026-10-01，作者测试了真实的 106,388 字节 Telegram PDF。Hermes 能读取文件并加载 `jai-receipts`，但无法可靠地将完整 Base64 放入远程 MCP 参数，没有提交成功。这触发了 [M15 Step 5](M15_zh.md) 已冻结的备用方案。安装后重新进行真实图片／PDF、原件 SHA-256、重放与手机链接检查；自动化检查不能替代验收。

[适配器](../../../integrations/hermes/jai-ledger/receipt_adapter.py) 是 Hermes 管理的本地 stdio MCP 子进程。它接受原始附件路径、文件名与 MIME 类型，向 `POST /api/v1/agent/receipt-inbox` 流式上传 multipart。无需新容器或修改 Hermes 源码。接口在解析前检查相同 bearer token 与 `receipt:intake` scope，即使没有 Content-Length 也限制请求体大小，并调用远程 MCP 使用的同一 M14 校验、RLS、原子收件／重放服务。文件字节与 Base64 不进入模型的参数或结果。

## 每次只做一个安装动作

以下命令匹配作者的 **`accountant`** profile 和已确认的**开发端口 8000**。相对仓库的命令从项目根目录执行。不要操作生产应用、数据库或默认 Hermes 网关。

1. 重建开发应用以加载新接口，没有新迁移：

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build app
   ```

2. 让 Hermes 只报告当前原始附件的完整缓存路径与目录，不编码、不提交、不复制。确认主机可见的真实路径。本机只读检查的 Hermes 源码使用 profile 下的 `cache/documents` 与 `cache/images`，旧 `document_cache`／`image_cache` 目录也可能存在。只批准包含所需原件的具体附件缓存根目录，例如两类原件确实都位于 profile 的 `cache` 时使用它。不得批准 `/`、`/tmp`、HOME、整个 profile 或包含凭据的目录，不得猜测容器／主机路径映射或编造路径。

3. 安装适配器和更新后的英文 skill。保留已经安装的请求编号助手；仅修改 skill 安装副本中的助手路径：

   ```sh
   install -m 700 integrations/hermes/jai-ledger/receipt_adapter.py \
     "$HOME/.hermes/profiles/accountant/helpers/receipt_adapter.py"
   sed 's@/profiles/jai-ledger/@/profiles/accountant/@g' \
     integrations/hermes/jai-ledger/SKILL.md \
     > "$HOME/.hermes/profiles/accountant/skills/jai-receipts/SKILL.md"
   ```

4. 在 profile 私有 `.env` 中保留 token 与 Telegram 设置，添加以下非秘密值，路径必须为**真实绝对路径**：

   ```dotenv
   JAI_BASE_URL=http://127.0.0.1:8000
   JAI_ADAPTER_SCRIPT=/actual/home/.hermes/profiles/accountant/helpers/receipt_adapter.py
   JAI_ATTACHMENT_CACHE_ROOT=/verified/attachment/cache/root
   ```

   适配器需要 Python 3.12+ 和 `uv`。脚本将 MCP 2.2.0 与 httpx 0.28.1 固定在独立脚本环境中，不修改 Hermes 的 Python 环境，首次启动可能安装依赖。默认每文件 10 MiB，与 JAI 一致。如果修改 JAI 的 `MAX_RECEIPT_BYTES`，应在适配器环境设置对应 `JAI_MAX_RECEIPT_BYTES`，并在配置的 `env` 显式转发。

5. 将 [config-receipts-local_zh.yaml](../../../integrations/hermes/jai-ledger/config-receipts-local_zh.yaml) 合并到此 profile 的 `config.yaml`，只保留一个 `mcp_servers`，保留 provider／model 设置。远程 `jai` 暴露 `get_receipt_status`，本地 `jai_receipts` 暴露 `submit_receipt`。从当前 include 列表移除远程 Base64 `submit_receipt`。完整工作流保留其他 15 个已授权远程工具并添加相同本地条目。只读检查确认 Hermes 配置加载器也递归展开 `env` 中的 `${VAR}`；仅显式转发所需 JAI 值。不得将 token 粘贴到 YAML、命令参数或聊天。

6. 只验证并应用到此 profile：

   ```sh
   hermes -p accountant mcp test jai_receipts
   hermes -p accountant gateway restart
   ```

   本地诊断应发现一个工具。即使客户端 include 注册较少工具，远程 `mcp test jai` 仍可能发现全部 16 个服务端工具。

7. 加载更新后的已有 `jai-receipts` skill。如果 `/jai-receipts` 未识别，用自然语言要求通过 `skill_view` 读取；只缺少菜单项或加载提示不能说明失败。如果上下文仍保留旧规则，开启新会话。分别以 Telegram 文件／文档方式发送一张图片和一份 PDF，并明确要求提交。Hermes 应运行操作专属的请求编号助手，以简短原件路径参数调用 `mcp_jai_receipts_submit_receipt`，随后调用远程 `mcp_jai_get_receipt_status`。按 Hermes 的配置策略，助手仍可能需要终端审批；不要仅为走查关闭全局审批。

8. 继续原有[部署自测](M15-hermes-telegram_zh.md)：存储原件必须与源文件 SHA-256 一致，每笔采购只产生一个待审核条目，同事件原样重试必须重放，当前 AI 状态准确，返回审阅链接可在手机打开。JAI 网页链接的 `BASE_URL` 与适配器 loopback `JAI_BASE_URL` 仍分开。只有所有者能在网页确认。

## 错误与边界

- `JAI_ATTACHMENT_REJECTED`：检查获批目录、真实路径、类型与大小。适配器拒绝目录穿越、任意路径层级的符号链接、硬链接、设备／FIFO、目录、空文件和超限文件。不得扩大目录或复制任意文件绕过拒绝。
- `JAI_SUBMISSION_OUTCOME_UNKNOWN`：响应不可用／丢失，提交可能已经持久化。应说明结果未知，只能用完全相同的 request ID、source、文件名、MIME 类型和原始字节重试；不得生成新 ID 或断言失败。确定的认证／校验／冲突错误保留 MCP 错误码。
- 稳定 ID 仍需要真实可信会话／消息元数据。长期运行的 stdio 服务无法继承未来 Telegram 回合的环境，必须用现有每回合助手并原样传入输出，不得编造缺失来源坐标。
- 相册仍需要可信完整聚合器；适配器不添加聚合或降低检查。
- 此修复移除了模型复制 Base64 的环节。总时间仍包含 Telegram 下载、模型选择工具、已配置终端审批与 JAI 现有 AI 提取，不承诺固定响应时间。
- 没有新增 Agent 确认、费用过账、草稿生命周期操作或额外记账权限。
