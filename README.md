# zcode-relay-models

> ZCode Desktop 插件:给中转站(OpenAI / Anthropic 兼容 API)供应商做**模型自动检索** —— 像 dsh / ccs 内置供应商一样,加模型不用再手抄 ID,思考强度自动配好。

## 解决什么问题

在 ZCode 里添加中转站供应商时,内置供应商(dsh / ccs / Z.ai 等)有现成的模型列表,而自定义中转站要**手动逐个填写模型 ID**,还要自己试思考强度档位。本插件一条 `/relay` 命令:

1. 自动请求中转站的 `/models` 接口拉取全部可用模型;
2. 自动探测 OpenAI / Anthropic 兼容协议并写入供应商配置;
3. 自动同步面板模板，重启后可在模型设置中直接勾选;
4. 写入前自动备份 `provider_config.json`,原子替换,绝不写坏。

## v0.3.0:自动发现模型并同步面板勾选(重点)

逆向了 ZCode 桌面端供应商面板:内置 dsh/ccs 的"自动弹模型列表"其实来自 `zcode-builtin.json`(runtime 缓存)里模板的**静态 `builtinModelIds`**,全 App 没有任何动态 `/models` 拉取。`sync-template` 子命令把你的中转站做成同款模板:

- **面板原生勾选**:重启后在"模型设置"点该供应商,像 dsh/ccs 一样看到全部模型;**模板里新出现的模型默认关闭**,手动打开开关即可;
- **思考强度档位**:通过 `providerSiteRules`(`baseUrlMatch + apiTypeMatch` 双重限定,只命中该站)注入官方同款档位,并自动映射请求参数 —— OpenAI 兼容站注入 `reasoning.effort`,Anthropic 兼容站注入 `thinking.adaptive + output_config.effort`;
- **幂等可恢复**:每次写入自动备份、`revision` +1 防云端刷新覆盖;App 更新后列表消失就重跑一次 `sync-template`。

```bash
python $S sync-template --provider 卡扣ai                # 拉取并做成模板
python $S sync-template --provider 卡扣ai --use-config   # 不联网,用已存列表
python $S sync-template --provider 卡扣ai --reasoning gpt # 自定义档位
```

添加新供应商时，`/relay` 会自动发现模型并同步模板；不需要手抄模型 ID。重启 ZCode 后，在模型设置面板里像 dsh/ccs 一样勾选。注意：ZCode 原生“添加供应商”表单不会被插件监听，自动发现入口是 `/relay`。


### 只填 URL + Key 的新增流程

```text
/relay
→ 输入中转站 Base URL
→ 输入 API Key
→ 插件请求 /models，自动识别协议和全部模型
→ 重启 ZCode，在模型设置面板直接勾选模型
```

模型条目没有明确 reasoning 能力字段时，插件不会猜测或补写 `none/low/medium/high/xhigh/max`；只有远端明确返回的共同档位才会显示。`--name`、`--models`、`--reasoning` 都是高级覆盖，不是必填项。
## 特性

- **双协议自动探测**:OpenAI 兼容(`Authorization: Bearer`)与 Anthropic 兼容(`x-api-key`)都支持;baseUrl 不带 `/v1` 也能自动试 `/v1/models`、`/models`、根域名 `/v1/models`
- **思考强度预设**:`gpt`(disabled/low/high/max)、`gemini`(disabled/low/medium/high/xhigh/max)、自定义逗号列表、或不配置
- **测活**:`test` 子命令用最小补全请求逐个验证模型可用性(能直接报出 key 过期、模型下线)
- **安全写入**:每次写配置先落 `provider_config.json.bak-relay-<时间戳>` 备份,临时文件 + 原子 `os.replace`
- **零启动开销**:无 hook、无 MCP、无后台进程;不敲 `/relay` 时只是几个 Markdown 文件,不参与启动
- **纯 Python 标准库**:无第三方依赖,Python 3.8+ 即可

## 安装

### 方式一:本地市场(推荐)

1. `git clone` 本仓库(或下载解压);
2. ZCode → **插件市场 → 添加 → 添加插件市场**,粘贴仓库目录(即 `marketplace.json` 所在目录);
3. **个人 → relay-models-market → 中转站模型检索 → 安装**,重启 ZCode。

### 方式二:只装脚本

插件本体是独立脚本,不装插件也能用:

```bash
python relay-models/scripts/relay_cli.py --help
```

## 使用

### 斜杠命令 `/relay`

```
/relay                     → 列出所有供应商,交互式新增
/relay 大肥鱼               → 更新已有供应商(名称/ID/baseUrl 都能匹配)
/relay https://xxx.com/v1  → 按地址新增
```

### CLI 直用

```bash
S=relay-models/scripts/relay_cli.py

python $S providers --json                                   # 现有供应商清单
python $S models --url https://relay.example.com/v1 --key sk-xxx --json   # 拉模型列表
python $S add --name 名字 --url https://relay.example.com/v1 --key sk-xxx \
  --api-type openai-chat-completions --models "m1,m2" --reasoning gpt     # 新增供应商
python $S set-models --provider new-provider-8 --models "m1,m2" --prune   # 更新模型
python $S test --provider new-provider-8 --model m1                        # 单模型测活
```

| 子命令 | 作用 |
|---|---|
| `providers` | 列出现有供应商(ID/名称/协议/地址/模型/思考强度) |
| `models` | 拉取中转站模型列表,支持 `--filter 正则`、`--insecure`(自签证书)、`--json` |
| `add` | 新增供应商,写 `providerOrder` + `providerRules` + `modelRules` 三处 |
| `set-models` | 更新已有供应商;`--add` 追加、`--prune` 清理已下架模型、`--reasoning` 留空沿用现有档位 |
| `sync-template` | 把中转站做成面板原生模板(勾选列表 + 思考强度档位),详见 v0.3.0 章节 |
| `test` | 最小补全请求测活,输出回复内容或 HTTP 错误详情 |

### 思考强度预设

| 预设 | 档位 |
|---|---|
| `gpt` | disabled / low / high / max |
| `gemini` | disabled / low / medium / high / xhigh / max |
| `none` | 不配置(模型无思考强度下拉) |
| 逗号列表 | 如 `disabled,low,high` |

## 配置文件

读写目标:`~/.zcode/v2/provider_config.json`(可用 `--config` 指定其他路径)。修改项:

- `config.providerOrder` — 供应商排序
- `config.providerConfigRules.providerRules` — 供应商本体(key / baseUrl / 协议 / 模型)
- `config.modelConfigRules.providerModelRules` — 每模型的启用状态与 `optionSpecs.reasoningLevel`

ZCode 桌面端不会热加载外部改动,写入后若模型面板没变化,**重启 ZCode** 即可。

## 常见问题

- **拉不到模型?** 中转站可能没开 `/models` 路由或 key 权限不足,看返回的 `errors` 字段;自签证书加 `--insecure`。
- **模型列表和实际可用不一致?** 那是中转站上游同步问题,用 `test` 逐个验,`set-models --prune` 清理失效项。
- **会动我其他供应商吗?** 只新增/修改目标供应商的条目,内置供应商(zai / bigmodel / deepseek 等)不碰。

## License

[MIT](LICENSE)
