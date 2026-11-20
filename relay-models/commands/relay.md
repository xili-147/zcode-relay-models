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
1. 若参数里已有 baseUrl 就用它,否则向用户要三样:中转站 baseUrl(通常以 `/v1` 结尾)、API Key、显示名称。缺什么问什么,一次问全。
2. API 类型:baseUrl 带 `/v1` 默认 openai;先按 openai 拉取,失败再按 anthropic 拉取,哪个通用哪个。
3. 运行:`python "$ROOT/scripts/relay_cli.py" models --url URL --key KEY [--filter 正则] [--insecure]`。把返回的模型按"序号<TAB>ID"表格贴给用户(超过 40 个只贴前 40 个并注明总数)。
4. 让用户回复要启用的模型:序号、区间(如 `1,3,5-9`)、关键词或 `all`(推荐 all,反正面板里可勾选)。回复后映射回模型 ID,不必再确认。
5. 用 AskUserQuestion 问思考强度预设:`官方全档`(none/low/medium/high/xhigh/max,推荐)、`gpt`(disabled/low/high/max)、`none`(不配)、`自定义`(让用户给逗号列表)。
6. 执行:
   ```bash
   python "$ROOT/scripts/relay_cli.py" add --name 名称 --url URL --key KEY \
     --api-type openai-chat-completions --models "m1,m2" --reasoning 官方全档或gpt
   ```
   (anthropic 站点用 `--api-type anthropic-messages`;要排在列表最上面加 `--position top`。)
7. **紧接着执行**(面板原生勾选列表的关键一步;模板里新出现的模型默认关闭,在面板里手动打开开关即可):
   ```bash
   python "$ROOT/scripts/relay_cli.py" sync-template --provider <返回的providerId>
   ```
8. 汇报:providerId、模型数、模板 ID、备份路径;提醒**重启 ZCode** 后在"模型设置"面板点该供应商即可像 dsh/ccs 一样勾选模型与思考强度。

**更新流程**(针对已有供应商):
1. 用 providers 输出展示该供应商当前模型与现有思考强度。
2. 用该供应商存储的 key 拉取远端列表(从 providers 的 baseUrl/apiType 取;key 需要时问用户)。
3. 对比展示:远端有但未启用 / 当前启用但远端没有,让用户决定启用哪些(默认建议:全选远端列表)。
4. 执行:
   ```bash
   python "$ROOT/scripts/relay_cli.py" set-models --provider ID --models "m1,m2" --prune
   python "$ROOT/scripts/relay_cli.py" sync-template --provider ID
   ```
   (`--reasoning` 留空沿用现有;`sync-template` 会刷新面板勾选列表与思考强度档位。)
5. 可选:对每个启用模型跑 `python "$ROOT/scripts/relay_cli.py" test --provider ID --model M` 测活,汇报每个模型的 OK/失败。

**输出纪律**:中间步骤只贴必要数据;最终汇报 5 行以内:providerId、启用模型数、模板 ID、备份路径、重启提示。
