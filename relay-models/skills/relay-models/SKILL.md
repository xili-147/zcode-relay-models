---
name: relay-models
description: 添加或更新 ZCode 中转站/OpenAI 兼容/Anthropic 兼容供应商时自动拉取该站点的模型列表(类似 dsh/ccs 的自动检索),交互式选择模型并配置思考强度(reasoningLevel),写入 provider_config.json。触发词:中转站、供应商、加模型、模型列表、自动检索、思考强度、relay、provider、models。
---

# relay-models:中转站模型自动检索与供应商配置

目标:让"添加中转站供应商"像内置 dsh/ccs 一样自动出模型列表,选中即写入,并配好思考强度档位。

## 脚本

`scripts/relay_cli.py`(Python 3 标准库,无依赖),位于本插件根目录。根目录 = 环境变量 `ZCODE_PLUGIN_ROOT`(回退 `CLAUDE_PLUGIN_ROOT`);都为空时用:

```bash
ROOT=$(dirname "$(find "$HOME/.zcode/cli/plugins/cache" -type f -name relay_cli.py 2>/dev/null | tail -1)/..")
```

配置文件默认 `~/.zcode/v2/provider_config.json`,所有写入前自动生成 `.bak-relay-时间戳` 备份,写入为原子替换。

## 子命令

```bash
# 列出现有供应商(ID/名称/baseUrl/类型/模型/思考强度)
python "$ROOT/scripts/relay_cli.py" providers --json

# 拉取中转站模型列表(自动尝试 /v1/models、/models、根域名 /v1/models;openai 失败自动按 anthropic 头重试由调用方处理)
python "$ROOT/scripts/relay_cli.py" models --url https://relay.example.com/v1 --key sk-xxx [--filter 正则] [--insecure] [--json]

# 新增供应商(写 providerOrder + providerRules + modelRules)
python "$ROOT/scripts/relay_cli.py" add --name 显示名 --url https://relay.example.com/v1 \
  --key sk-xxx --api-type openai-chat-completions \
  --models "m1,m2" --reasoning gpt [--position top]

# 更新已有供应商模型(整体替换;--add 追加;--prune 清掉已下架规则;--reasoning 留空沿用现有)
python "$ROOT/scripts/relay_cli.py" set-models --provider new-provider-7 --models "m1,m2" --reasoning gemini --prune

# 单模型测活(最小补全请求)
python "$ROOT/scripts/relay_cli.py" test --provider new-provider-7 --model glm-5.3
```

`--reasoning` 取值:`gpt` = disabled/low/high/max;`gemini` = disabled/low/medium/high/xhigh/max;`full` 同 gemini;`none` = 不配 optionSpecs;或逗号自定义列表。

## 流程

1. `providers --json` 摸清现状;参数匹配到已有供应商 → 更新,否则新增。
2. 新增:要齐 baseUrl / key / 名称三样(一次问全);按 openai 拉 `/models`,失败换 anthropic 头再试。
3. 模型列表以"序号<TAB>ID"展示,用户用序号/区间/关键词/`all` 选择,映射回 ID 后确认一次。
4. AskUserQuestion 问思考强度预设(推荐 `gpt`)。
5. `add` 或 `set-models` 落盘,汇报 providerId、模型数、备份路径;提示模型面板未刷新时重启 ZCode。
6. 大站模型多时用 `--filter` 缩小范围;可选 `test` 逐个测活再让用户挑。
