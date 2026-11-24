---
description: 自动检索中转站模型列表并添加/更新 ZCode 供应商与思考强度(面板原生弹出)
argument-hint: [供应商名|ID|baseUrl|关键词]
---

# /relay 中转站模型自动检索

按 `$ARGUMENTS` 执行。全程只做这件事,不要展开别的。

**脚本定位**:插件根目录 = 环境变量 `ZCODE_PLUGIN_ROOT`(回退 `CLAUDE_PLUGIN_ROOT`);若两者都为空,用 `find "$HOME/.zcode/cli/plugins/cache" -type d -name relay-models 2>/dev/null | tail -1` 取最新安装目录。脚本相对根目录为 `scripts/relay_cli.py`,用 `python` 运行。

**第一步**:运行 `python "$ROOT/scripts/relay_cli.py" providers --json` 列出现有供应商。

**判断流向**:
- `$ARGUMENTS` 能匹配到某个现有供应商(ID/名称/baseUrl 子串,忽略大小写)→ 走 **更新流程**;
- 否则走 **新增流程**(参数为空、或像 URL/新名字都算新增)。

**新增流程**:
1. 若参数里已有 baseUrl 就用它；否则一次向用户索取 Base URL 与 API Key。显示名称可省略，默认使用 URL hostname。
2. 运行 `add`，不传 `--models`：插件会先请求远端 `/models`，按 OpenAI / Anthropic 协议自动探测；拉取失败时不创建空供应商。
3. 只有远端明确返回的 reasoning 能力才写入思考档位；没有能力字段时不写 reasoning 规则，不猜测全档。
4. `add` 成功后自动写入 provider 配置和 `zcode-builtin.json` 模板。重启 ZCode，在模型设置面板点该供应商即可直接勾选模型。
5. 高级覆盖仍可用：`--name`、`--models`、`--reasoning`、`--position`；不作为普通新增的必填步骤。

插件在会话启动时会运行后台配置监听。原生供应商菜单保存新供应商，或修改已有供应商的 URL/API Key 后，监听器约 2 秒内请求 /models 并同步候选；新增模型默认关闭，原有开关保持不变。更新插件后需先开启一个新会话；若 ZCode 未热加载模板，完整重启后查看模型设置。
**更新流程**(针对已有供应商):
1. 用 providers 输出展示该供应商当前模型与现有思考强度。
2. 用该供应商存储的 key 拉取远端列表(从 providers 的 baseUrl/apiType 取;key 需要时问用户)。
3. 对比展示:远端有但未启用 / 当前启用但远端没有,让用户决定启用哪些(默认建议:全选远端列表)。
4. 执行:
   ```bash
   python "$ROOT/scripts/relay_cli.py" set-models --provider ID --models "m1,m2" --prune
   python "$ROOT/scripts/relay_cli.py" sync-template --provider ID
   ```
   (`--reasoning` 留空沿用现有;`sync-template` 会重新发现模型并刷新面板勾选列表。若远端没有能力字段,会删除该站点旧 reasoning 规则。)
5. 可选:对每个启用模型跑 `python "$ROOT/scripts/relay_cli.py" test --provider ID --model M` 测活,汇报每个模型的 OK/失败。

**输出纪律**:中间步骤只贴必要数据;最终汇报 5 行以内:providerId、启用模型数、模板 ID、备份路径、重启提示。
