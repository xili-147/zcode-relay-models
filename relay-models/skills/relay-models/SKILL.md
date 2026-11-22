---
name: relay-models
description: 添加或更新 ZCode 中转站/OpenAI 兼容/Anthropic 兼容供应商时自动拉取该站点的模型列表,交互式选择模型并配置思考强度,写入 provider_config.json 并同步为面板原生模板(像 dsh/ccs 一样在模型设置里直接勾选)。触发词:中转站、供应商、加模型、模型列表、自动检索、思考强度、面板、勾选、relay、provider、models。
---

# relay-models:中转站模型自动检索与供应商配置

目标:让"添加中转站供应商"像内置 dsh/ccs 一样 —— 模型列表直接在"模型设置"面板里勾选,思考强度自动带档位。

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

# 拉取中转站模型列表
python "$ROOT/scripts/relay_cli.py" models --url https://relay.example.com/v1 --key sk-xxx [--filter 正则] [--insecure] [--json]

# 新增供应商(空 models 自动发现;同时同步模板)
python "$ROOT/scripts/relay_cli.py" add --name 显示名 --url https://relay.example.com/v1 \
  --key sk-xxx --api-type openai-chat-completions \
  [--models "m1,m2"] [--reasoning gpt] [--position top]

# 更新已有供应商模型(整体替换;--add 追加;--prune 清掉已下架规则;--reasoning 留空沿用现有)
python "$ROOT/scripts/relay_cli.py" set-models --provider new-provider-7 --models "m1,m2" --prune

# 关键:同步为面板原生模板 —— 模型设置里像 dsh/ccs 一样弹列表勾选,并注入思考强度档位
python "$ROOT/scripts/relay_cli.py" sync-template --provider new-provider-7 [--models "m1,m2"] [--use-config] [--reasoning 官方全档|gpt|none|逗号列表]

# 单模型测活(最小补全请求)
python "$ROOT/scripts/relay_cli.py" test --provider new-provider-7 --model glm-5.3
```

`--reasoning` 取值:`gpt` = disabled/low/high/max;`gemini` = disabled/low/medium/high/xhigh/max;`full` 同 gemini;`none` = 不配 optionSpecs;或逗号自定义列表。显式传入模型时按该参数配置；`add` 或 `sync-template` 留空模型列表时,只写 `/models` 返回且被所有模型共同支持的 reasoning 等级,没有能力字段就删除该站点 reasoning 规则。`--use-config` 不联网,只使用已存模型列表；无已存模型时直接报错。

## sync-template 机制(为什么面板能弹列表)

ZCode 面板的"自动弹列表"来自 `zcode-builtin.json`(runtime 缓存:`~/.zcode/v2/runtime/provider/<平台>/<版本>/endpoint-*/zcode-builtin.json`,出厂兜底在安装目录 `resources/config/provider/zcode-builtin.json`)的**静态模板** `builtinModelIds` —— 内置 dsh/ccs/Z.ai 就是这样,全 App 没有任何动态 `/models` 拉取。`sync-template` 做三件事(全路径幂等,可反复跑):

1. 在 `templateRules` 追加中转站模板:`templateId = relay-<域名slug>`,同 slug 被占(同站不同 key/协议)则 hash(providerId) 后缀;
2. 在 `templateModelRules` 写每个模型的启用规则(**默认关闭**,仅供应商已启用的模型为 true,面板里手动开);
3. 在 `providerSiteRules` 追加一条按 `baseUrlMatch + apiTypeMatch` 双重限定的思考强度档位规则(`modelMatch` 自动合并,只命中该站);没有明确能力时删除该站点旧规则。

同时把该供应商在 `provider_config.json` 里的顶层 `templateId` 绑定到模板。每次写入自动备份,`revision` +1 防云端刷新覆盖。**若 App 更新或云端刷新后列表消失,重跑 `sync-template` 即可恢复。**

## 流程
1. 新增:只向用户索取 Base URL 与 API Key；名称默认按 URL hostname 生成。
2. `add` 留空 `--models` 时自动请求 `/models`，自动探测 OpenAI / Anthropic 协议；失败就停止，不写空供应商。
3. reasoning 只接受远端明确声明的共同能力；无能力字段就删除该站点 reasoning 规则，不补全预设。
4. `add` 自动同步模板；重启 ZCode 后，在模型设置面板里直接勾选已发现模型。
5. `--name`、`--models`、`--reasoning` 是高级覆盖。ZCode 原生添加供应商表单不会被插件监听，自动发现入口是 `/relay`。
