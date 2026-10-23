# term-fix

> 终端报错的那一秒，别再复制粘贴了。`tf npm install` —— 失败就地解释，并给你一条可执行的修复命令。

`term-fix` 是一个极简的终端包装 Agent：它执行你的命令，捕获 `stderr`，带上操作系统 / Shell / 工具链版本去问大模型，
然后在终端里直接给出「原因 + 一条修复命令」，你按一个回车就能执行。

```console
$ tf npm install
npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
npm ERR! While resolving: shop@1.0.0
npm ERR! Found: react@18.3.1
npm ERR! Could not resolve dependency:
npm ERR! peer react@"^17.0.2" from react-dom@17.0.2

exit 1 (12.4s)

╭────────────────────────────── term-fix ──────────────────────────────╮
│ cause  依赖树冲突：react-dom@17 要求 react@17，与已安装的 react@18 冲突。 │
│ fix    npm install react-dom@18 --save-exact                          │
╰────────── gemini/gemini-2.5-flash · risk low · confidence high ───────╯
Run the fix? [Y/n] y
running fix...
added 1 package, and audited 248 packages in 2s
fix finished (exit 0)
```

<!-- 录制 10 秒终端动图后放到 docs/demo.gif，并在此处取消注释：
![demo](docs/demo.gif)
-->

## 为什么不用聊天窗口

| | 复制粘贴到网页 | term-fix |
|---|---|---|
| 出错到拿到答案 | 切窗口、贴日志、描述环境 | 一个回车 |
| 环境信息 | 靠你回忆（macOS？哪个 JDK？） | 自动带上 OS/Shell/工具链版本 |
| 密钥泄露 | 日志里的 token 会一起贴出去 | 上传前本地脱敏 |
| 执行 | 再切回来手动跑 | 就地确认并执行，退出码原样返回 |

## 安装

```bash
pipx install term-fix        # 推荐：隔离环境、全局可用
# 或
pip install --user term-fix
# 或在仓库里开发
pip install -e ".[dev]"
```

装完就有两个等价的命令：`tf` 和 `term-fix`。

## 配置模型

任选一种，`tf --doctor` 会告诉你当前用的是哪一个：

```bash
export GEMINI_API_KEY=...        # https://aistudio.google.com/apikey（默认模型 gemini-2.5-flash）
export OPENAI_API_KEY=...        # 任何 OpenAI 兼容服务
export TERM_FIX_BASE_URL=http://localhost:11434/v1   # 本地 Ollama / vLLM / LM Studio
```

也可以写进 `~/.term-fix/config.toml`（或项目根目录的 `.term-fix.toml`，优先级更高）：

```toml
provider = "gemini"        # auto | gemini | openai
model = "gemini-2.5-flash"
lang = "zh"                # 解释的语言，zh / en / ja ...
max_error_chars = 6000     # 只把 stderr 末尾这么多字符发给模型
timeout = 60
```

## 用法

```bash
tf npm install                                # 跑命令；成功就什么都不做
tf --yes npm run build                        # 安全修复自动执行，不再问
tf --dry-run mvn -q package                   # 只看解释，绝不执行
tf --capture-all pytest -x                    # 连 stdout 一起捕获（有些工具把错误写到 stdout）
tf --model gemini-2.5-pro docker compose up   # 换个模型
tf --provider openai --base-url http://localhost:11434/v1 --model qwen2.5-coder ollama run llama3
tf --doctor --check                           # 体检 + 一次真实调用
tf --history                                  # 最近 10 次诊断
```

**参数边界（重要）**：`tf` 自己的开关只写在命令之前。从第一个非选项词开始，后面所有内容原样传给命令：

```bash
tf apt install -y foo      # -y 属于 apt，不会被 tf 吞掉
tf --yes apt install foo   # 这个 --yes 属于 tf
tf -- ls -la               # 用 -- 强制分隔
```

| 开关 | 作用 |
|---|---|
| `-y, --yes` | 低/中风险修复直接执行（高风险仍会问） |
| `--dry-run` | 只解释，不执行任何修复 |
| `--force` | 允许写命令在内的修复非交互执行 |
| `--capture-all` | 同时捕获 stdout（代价：子进程看不到 TTY，彩色/进度条会消失） |
| `-q, --quiet` | 只输出诊断面板 |
| `--provider/--model/--base-url` | 覆盖本次使用的模型 |
| `--lang` | 解释语言 |
| `--max-chars` | 上传的 stderr 上限（取末尾） |
| `--doctor [--check]` | 打印配置与连通性 |
| `--history [--limit N]` | 历史记录 |

退出码即语义：命令成功 `0`；失败且没执行修复 → 原命令的退出码；执行了修复 → 修复命令的退出码；被 Ctrl-C → `130`。

## 它怎么工作

```
tf <cmd>
  │
  ├─ 执行 <cmd>：stdout 直通终端（保留颜色/进度条），stderr 边转发边留一份
  │
  ├─ 退出码 0 ────────────────────────────► 静默结束
  │
  └─ 退出码 ≠ 0
        ├─ 取 stderr 末尾 N 字符 → 脱敏 → 附带 OS/Shell/cwd/工具版本/项目文件
        ├─ 调模型（要求严格 JSON：explanation / fix_command / confidence / notes）
        ├─ 渲染面板；本地按危险等级给命令打分
        └─ 询问 [Y/n] → 用你的 shell 执行 → 记录到 ~/.term-fix/history.jsonl
```

发给模型的内容长这样（这就是它比手贴日志准的原因）：

```
## Environment
os: macOS 26.1 (arm64)
shell: zsh
cwd: /Users/dev/shop
project files: package.json, package-lock.json
tool versions: npm 11.2.0

## Failed command
`npm install`  → 退出码 1

## Error output (stderr)
npm ERR! code ERESOLVE ...
```

## 安全设计

让模型决定执行什么命令是危险的，所以有四道闸门：

1. **本地脱敏**：`sk-*`、`ghp_*`、`AKIA*`、`Bearer`、URL 里的 `user:pass`、`token=...` 等在**上传前**替换成 `***`。
2. **危险等级打分**：不只匹配 `rm -rf`，而是看*目标*——`rm -rf node_modules` 是中风险（可重建产物），`rm -rf src/` 高风险，`rm -rf ~/` 或 `/` 直接拒绝。
3. **确认策略**：低/中风险问一次；高风险即使 `--yes` 也要确认；写操作级别必须 `--force` + 交互确认，非 TTY 一律拒绝。
4. **失败开放**：模型挂了、没配置 key、命令没输出——都只打印原因并以原退出码结束，不会破坏你的构建脚本。

历史记录只写命令、解释和修复命令，**不会**落盘原始 stderr。文件权限 `0600`。

## 支持哪些模型

| provider | 说明 |
|---|---|
| `gemini` | 默认，`google-genai` SDK，`response_mime_type=application/json` 保证结构化输出 |
| `openai` | 任何 OpenAI 兼容的 `/chat/completions`：OpenAI、DeepSeek、通义、Kimi、Groq、Ollama、vLLM、LM Studio |

本地 Ollama 示例（不需要 key）：

```bash
export TERM_FIX_BASE_URL=http://localhost:11434/v1
export TERM_FIX_MODEL=qwen2.5-coder:7b
tf --provider openai ls /nope
```

## 开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # 81 个测试，全部离线，不需要 API key
```

```
term_fix/
├── cli.py        # 参数分区、流程编排、rich 渲染
├── executor.py   # 子进程执行：stdout 直通、stderr 边转发边截尾、Ctrl-C/超时
├── ai.py         # prompt 构造、双 provider、JSON 解析（含容错回退）
├── context.py    # OS/发行版/Shell/工具版本/项目文件探测
├── safety.py     # 脱敏 + 危险命令分级
├── config.py     # 配置合并：默认 < 配置文件 < 环境变量 < 命令行
└── history.py    # JSONL 历史
```

## 已知限制

- **不是 shell**：`tf cd foo` 不会改变你的目录，`export` 也不会留下。要跑整段脚本请 `tf bash -c '...'`。
- **stderr 是管道**：被包装命令的 stderr 看不到 TTY，少数工具的彩色输出会退化（stdout 不受影响）。
- **一次修复，不自动重跑**：修复执行后不会自动重跑原命令，避免重复跑昂贵的构建（见 Roadmap）。
- **交互式命令**（`vim`、`top`、需要输入密码的 `sudo`）能跑，但失败后的修复建议通常价值不大。
- 只把末尾 `max_error_chars` 字符发给模型，超长日志的前半段会被省略（prompt 里会注明省略了多少）。

## Roadmap

- [ ] `tf --retry`：修复后自动重跑原命令并二次诊断
- [ ] 本地缓存「同一条错误 + 同一环境」的诊断结果，省掉重复调用
- [ ] shell hook（zsh/bash `preexec`）：连 `tf` 前缀都不用敲
- [ ] 团队共享的 fix 规则文件（`term-fix.toml`）

## License

MIT

---

## English

**term-fix** wraps a shell command, captures its stderr, sends it (with OS / shell / toolchain
context, secrets redacted) to an LLM, and offers a one-line fix you can approve with a single
Enter. Model-generated commands are scored locally by blast radius: `rm -rf node_modules` is
medium, `rm -rf ~/` is refused outright.

Works with Gemini or any OpenAI-compatible endpoint (including local Ollama). stdout stays
attached to your terminal, so colours and progress bars keep working, and the wrapped command's
exit code is what you get back.

```bash
pipx install term-fix
export GEMINI_API_KEY=...
tf npm install
```
