#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relay_cli.py — ZCode 中转站供应商工具:拉取模型列表、添加/更新供应商与思考强度。

仅用 Python 标准库。配置文件: ~/.zcode/v2/provider_config.json
子命令:
  providers   列出现有供应商
  models      从中转站拉取模型列表
  add         新增供应商(写入 providerOrder/providerRules/modelRules)
  set-models  更新已有供应商的模型与思考强度
  test        用最小补全请求测活某个模型
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG = Path.home() / ".zcode" / "v2" / "provider_config.json"

REASONING_PRESETS = {
    "gpt": ["disabled", "low", "high", "max"],
    "gemini": ["disabled", "low", "medium", "high", "xhigh", "max"],
    "full": ["disabled", "low", "medium", "high", "xhigh", "max"],
}

OPENAI_REASONING_MAP = '{\n  "reasoning": {\n    "effort": reasoningLevel\n  }\n}'
ANTHROPIC_REASONING_MAP = ('{\n  "thinking": {\n    "type": "adaptive"\n  },\n'
                           '  "output_config": {\n    "effort": reasoningLevel\n  }\n}')


def builtin_json_paths():
    """生效的 zcode-builtin.json(runtime 各版本缓存 + 出厂兜底)。"""
    import glob
    import os
    env = os.environ.get("RELAY_BUILTIN_DIR")
    if env:
        return sorted(glob.glob(str(Path(env) / "*.json")))
    pats = [
        str(Path.home() / ".zcode" / "v2" / "runtime" / "provider" / "*" / "*" / "endpoint-*" / "zcode-builtin.json"),
        r"D:\glm5.3官方harness\ZCode\resources\config\provider\zcode-builtin.json",
    ]
    out = []
    for p in pats:
        out.extend(glob.glob(p))
    return out


def reasoning_map_for(api_type):
    return (ANTHROPIC_REASONING_MAP if api_type == "anthropic-messages"
            else OPENAI_REASONING_MAP)


def regex_escape(s):
    return re.escape(s)


def merge_model_match(old_re, models):
    """合并 providerSiteRules 的 modelMatch 正则(仅解析自己生成的 ^(?:...)$ 形式)。"""
    m = re.match(r"^\^\(\?:([\s\S]+)\)\$$", old_re or "")
    alt = set(m.group(1).split("|")) if m else set()
    alt |= {regex_escape(x) for x in models}
    return "^(?:" + "|".join(sorted(alt)) + ")$"


def upsert_relay_template(builtin, tid, name, api_type, base, models, values,
                          enabled_ids=None):
    """在 zcode-builtin.json dict 里 upsert 模板+模板模型规则+站点规则。

    enabled_ids:默认开启的模型(= 供应商已启用的 modelOrder);其余模板模型默认关闭。"""
    c = builtin["config"]
    trules = c["providerConfigRules"]["templateRules"]
    entry = None
    for t in trules:
        if t.get("templateId") == tid:
            entry = t
            break
    tpl = {
        "templateId": tid,
        "templateNameMap": {"zh-CN": name},
        "config": {
            "access": {"type": "api-key"},
            "api": {"type": api_type, "baseUrl": base},
            "builtinModelIds": list(models),
        },
    }
    if entry is None:
        trules.append(tpl)
    else:
        trules[trules.index(entry)] = tpl

    mc = c["modelConfigRules"]
    tmr = mc["templateModelRules"]
    tmr[:] = [r for r in tmr if r.get("templateId") != tid]
    enabled_set = set(enabled_ids or [])
    for mid in models:
        tmr.append({"templateId": tid, "modelId": mid,
                    "config": {"enabled": mid in enabled_set}})

    sr = mc["providerSiteRules"]
    base_re = regex_escape(base) + r"/?"
    rule_cfg = None
    if values:
        rule_cfg = {"optionSpecs": {"reasoningLevel": {
            "values": list(values), "map": reasoning_map_for(api_type)}}}
    hit = None
    for r in sr:
        if r.get("baseUrlMatch") == base_re and r.get("apiTypeMatch") == api_type:
            hit = r
            break
    if rule_cfg is None:
        if hit is not None:
            sr.remove(hit)
    elif hit is None:
        sr.append({"modelMatch": merge_model_match("", models),
                   "baseUrlMatch": base_re,
                   "apiTypeMatch": api_type, "config": rule_cfg})
    else:
        hit["modelMatch"] = merge_model_match(hit.get("modelMatch", ""), models)
        hit["config"] = rule_cfg


def provider_name_from_url(url):
    parsed = urlsplit(normalize_base(url))
    return parsed.hostname or re.sub(r"^https?://", "", normalize_base(url)).split("/")[0]


def sync_template_config(cfg, rule, config_path, models, values):
    conf = rule["config"]
    api = conf.get("api", {})
    base = normalize_base(api.get("baseUrl", ""))
    api_type = api.get("type", "openai-chat-completions")
    name = rule.get("providerName") or rule["providerId"]
    pid = rule["providerId"]
    if not base:
        raise ValueError("该供应商无 baseUrl(内置模板供应商跳过)")

    parsed = urlsplit(base)
    host = (parsed.hostname or "relay").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "relay"
    prev = rule.get("templateId")
    if prev and prev.startswith("relay-"):
        tid = prev
    else:
        import hashlib
        tid = f"relay-{slug}"
        taken = set()
        for p in builtin_json_paths():
            try:
                d = load_config(Path(p))
                for t in d["config"]["providerConfigRules"]["templateRules"]:
                    taken.add(t.get("templateId"))
            except Exception:
                pass
        if tid in taken:
            tid += "-" + hashlib.sha1(pid.encode()).hexdigest()[:8]

    written, failed = [], []
    for p in builtin_json_paths():
        try:
            bp = Path(p)
            d = load_config(bp)
            upsert_relay_template(d, tid, name, api_type, base, models, values,
                                  enabled_ids=conf.get("modelOrder", []))
            d["revision"] = int(d.get("revision", 0)) + 1
            bak = backup(bp)
            save_config(bp, d)
            written.append({"path": str(bp), "backup": str(bak)})
        except Exception as e:
            failed.append({"path": str(p), "error": f"{type(e).__name__}: {e}"})

    if not written:
        raise RuntimeError("未找到可写入的 zcode-builtin.json 模板文件")

    rule["templateId"] = tid
    bak = backup(config_path)
    save_config(config_path, cfg)
    return tid, written, failed, bak


def cmd_sync_template(args):
    cfg = load_config(args.config)
    try:
        rule = find_rule(cfg, args.provider)
    except ValueError as e:
        out_json({"ok": False, "error": str(e)})
        sys.exit(2)
    if not rule:
        out_json({"ok": False, "error": f"找不到供应商: {args.provider}"})
        sys.exit(2)
    conf = rule["config"]
    api = conf.get("api", {})
    base = normalize_base(api.get("baseUrl", ""))
    api_type = api.get("type", "openai-chat-completions")
    key = conf.get("access", {}).get("apiKey", "")
    name = rule.get("providerName") or rule["providerId"]
    pid = rule["providerId"]
    if not base:
        out_json({"ok": False, "error": "该供应商无 baseUrl(内置模板供应商跳过)"})
        sys.exit(2)

    models = split_models(args.models)
    discovered = not models and not args.use_config
    discovered_levels = None
    errs = []
    if discovered:
        _, models, discovered_levels, _protocol, errs = discover_models(
            base, key, "openai" if "openai" in api_type else "anthropic",
            args.timeout, args.insecure)
    if not models:
        models = list(conf.get("modelOrder", []))
    if not models:
        out_json({"ok": False, "error": "拉取失败且无已存模型", "detail": errs})
        sys.exit(2)

    if args.reasoning:
        values = resolve_reasoning(args.reasoning)
    else:
        values = discovered_levels if discovered else None
    tid, written, failed, bak2 = sync_template_config(
        cfg, rule, args.config, models, values)
    out_json({"ok": True, "templateId": tid, "providerId": pid,
              "providerName": name, "models": models, "reasoning": values,
              "written": written, "failed": failed,
              "providerConfigBackup": str(bak2)})


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_config(path, data):
    tmp = path.with_name(path.name + ".tmp-relay")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def backup(path):
    b = path.with_name(path.name + ".bak-relay-" + time.strftime("%Y%m%d%H%M%S"))
    b.write_bytes(path.read_bytes())
    return b


def normalize_base(url):
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url


def ssl_ctx(insecure):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def http_json(url, headers, timeout, insecure=False):
    req = urllib.request.Request(
        url, headers={"User-Agent": "zcode-relay-cli/0.1", **headers}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx(insecure)) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


REASONING_KEYS = ("reasoning_levels", "reasoningLevels", "thinking_levels", "thinkingLevels")


def _explicit_levels(item):
    if not isinstance(item, dict):
        return None
    for key in REASONING_KEYS:
        value = item.get(key)
        if isinstance(value, list):
            levels = [str(level) for level in value if isinstance(level, (str, int, float))]
            return levels or None
    for key in ("capabilities", "reasoning", "thinking"):
        value = item.get(key)
        if isinstance(value, dict):
            levels = _explicit_levels(value)
            if levels:
                return levels
    return None


def parse_reasoning_levels(obj):
    """Return levels explicitly supported by every listed model, never a guessed preset."""
    if isinstance(obj, dict):
        items = obj.get("data") or obj.get("models") or []
        global_levels = _explicit_levels(obj)
    elif isinstance(obj, list):
        items = obj
        global_levels = None
    else:
        return None

    model_items = [item for item in items if isinstance(item, dict)]
    if not model_items:
        return global_levels
    per_model = [_explicit_levels(item) for item in model_items]
    if any(levels is None for levels in per_model):
        return global_levels
    common = set(per_model[0])
    for levels in per_model[1:]:
        common.intersection_update(levels)
    ordered = [level for level in per_model[0] if level in common]
    return ordered or global_levels


def parse_model_catalog(obj):
    ids = []
    if isinstance(obj, dict):
        items = obj.get("data") or obj.get("models") or []
    elif isinstance(obj, list):
        items = obj
    else:
        items = []
    for it in items:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
            if mid:
                ids.append(str(mid))
    seen, uniq = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq, parse_reasoning_levels(obj)


def parse_model_ids(obj):
    return parse_model_catalog(obj)[0]


def model_candidates(base):
    cands = []
    if re.search(r"/v\d+$", base):
        cands.append(base + "/models")
        cands.append(re.sub(r"/v\d+$", "", base) + "/v1/models")
    else:
        cands.append(base + "/v1/models")
        cands.append(base + "/models")
    origin = re.sub(r"^(https?://[^/]+).*$", r"\1", base)
    cands.append(origin + "/v1/models")
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def fetch_models_detail(base, key, protocol, timeout, insecure=False):
    base = normalize_base(base)
    if protocol == "anthropic":
        headers = {"x-api-key": key or "", "anthropic-version": "2023-06-01",
                   "Accept": "application/json"}
    else:
        headers = {"Authorization": "Bearer " + (key or ""),
                   "Accept": "application/json"}
    errs = []
    for url in model_candidates(base):
        try:
            payload = http_json(url, headers, timeout, insecure)
            ids, levels = parse_model_catalog(payload)
            if ids:
                return url, ids, levels, None
            errs.append(f"{url}: 200 但无模型条目")
        except urllib.error.HTTPError as e:
            errs.append(f"{url}: HTTP {e.code}")
        except Exception as e:
            errs.append(f"{url}: {type(e).__name__}: {e}")
    return None, [], None, errs


def discover_models(base, key, api_type, timeout, insecure=False):
    """Prefer the configured API protocol, then try the alternate protocol."""
    preferred = "openai" if api_type == "auto" or "openai" in api_type else "anthropic"
    errors = []
    for protocol in (preferred, "anthropic" if preferred == "openai" else "openai"):
        endpoint, ids, levels, errs = fetch_models_detail(base, key, protocol, timeout, insecure)
        if ids:
            return endpoint, ids, levels, protocol, None
        errors.extend(errs or [])
    return None, [], None, preferred, errors


def find_rule(cfg, ident):
    """按 id 或名称匹配供应商规则;名称模糊命中多个时抛 ValueError。"""
    rules = cfg["config"]["providerConfigRules"]["providerRules"]
    ident_l = (ident or "").strip().lower()
    if not ident_l:
        return None
    for r in rules:
        if r.get("providerId", "").lower() == ident_l:
            return r
    hits = [r for r in rules if ident_l in r.get("providerName", "").lower()]
    if len(hits) > 1:
        raise ValueError("名称匹配到多个供应商: " +
                         ", ".join(f"{r['providerId']}({r.get('providerName')})" for r in hits))
    return hits[0] if hits else None


def find_by_url(cfg, url):
    nb = normalize_base(url)
    for r in cfg["config"]["providerConfigRules"]["providerRules"]:
        if normalize_base(r.get("config", {}).get("api", {}).get("baseUrl", "")) == nb:
            return r
    return None


def provider_info(r, cfg):
    c = r.get("config", {})
    api = c.get("api", {})
    mconf = cfg["config"]["modelConfigRules"]["providerModelRules"]
    levels = {}
    for m in mconf:
        if m.get("providerId") == r.get("providerId"):
            lv = (m.get("config", {}).get("optionSpecs", {}) or {}).get("reasoningLevel")
            if lv:
                levels[m["modelId"]] = lv.get("values", [])
    return {
        "providerId": r.get("providerId"),
        "providerName": r.get("providerName"),
        "templateId": r.get("templateId"),
        "apiType": api.get("type"),
        "baseUrl": api.get("baseUrl"),
        "models": c.get("modelOrder", []),
        "reasoning": levels,
    }


def next_provider_id(cfg):
    order = cfg["config"].get("providerOrder", [])
    mx = 0
    for pid in order:
        m = re.match(r"^new-provider(?:-(\d+))?$", pid)
        if m:
            mx = max(mx, int(m.group(1) or 1))
    return f"new-provider-{mx + 1}" if mx else "new-provider"


def resolve_reasoning(spec):
    if not spec or spec == "none":
        return None
    if spec in REASONING_PRESETS:
        return REASONING_PRESETS[spec]
    vals = [v.strip() for v in spec.split(",") if v.strip()]
    return vals or None


def split_models(s):
    return [m.strip() for m in re.split(r"[,\n]+", s or "") if m.strip()]


def upsert_model_rules(cfg, provider_id, models, values, prune=False,
                       touch_reasoning=True):
    mconf = cfg["config"]["modelConfigRules"]["providerModelRules"]
    keep = set(models)
    if prune:
        mconf[:] = [m for m in mconf
                    if not (m.get("providerId") == provider_id
                            and m.get("modelId") not in keep)]
    for mid in models:
        entry = None
        for m in mconf:
            if m.get("providerId") == provider_id and m.get("modelId") == mid:
                entry = m
                break
        if entry is None:
            entry = {"modelId": mid, "providerId": provider_id, "config": {}}
            mconf.append(entry)
        conf = entry.setdefault("config", {})
        conf["enabled"] = True
        if not touch_reasoning:
            continue
        specs = conf.setdefault("optionSpecs", {})
        if values:
            specs["reasoningLevel"] = {"values": list(values)}
        else:
            specs.pop("reasoningLevel", None)
            if not specs:
                conf.pop("optionSpecs", None)


def cmd_providers(args):
    cfg = load_config(args.config)
    rules = cfg["config"]["providerConfigRules"]["providerRules"]
    infos = [provider_info(r, cfg) for r in rules]
    if args.json:
        out_json(infos)
        return
    for i in infos:
        print(f"[{i['providerId']}] {i['providerName']} | {i['apiType'] or '内置'} "
              f"| {i['baseUrl'] or '-'} | 模型 {len(i['models'])}")


def cmd_models(args):
    url, ids, _levels, _protocol, errs = discover_models(
        args.url, args.key, args.api_type, args.timeout, args.insecure)
    if args.filter:
        rx = re.compile(args.filter, re.I)
        ids = [i for i in ids if rx.search(i)]
    if not ids:
        out_json({"ok": False, "errors": errs or ["未获取到模型"]})
        sys.exit(2)
    if args.json:
        out_json({"ok": True, "endpoint": url, "count": len(ids), "models": ids})
    else:
        for n, i in enumerate(ids, 1):
            print(f"{n}\t{i}")


def cmd_add(args):
    cfg = load_config(args.config)
    dup = find_by_url(cfg, args.url)
    if dup:
        out_json({"ok": False,
                  "error": f"{args.url} 已被供应商 {dup['providerId']}({dup.get('providerName')}) 使用,请用 set-models"})
        sys.exit(2)
    pid = args.provider_id or next_provider_id(cfg)
    models = split_models(args.models)
    values = None
    discovered_levels = None
    if not models:
        requested_api = args.api_type or "auto"
        _, models, discovered_levels, detected_protocol, errs = discover_models(
            args.url, args.key, requested_api, args.timeout, args.insecure)
        if not models:
            out_json({"ok": False, "error": "未获取到模型，未创建供应商", "detail": errs or []})
            sys.exit(2)
        args.api_type = ("openai-chat-completions" if detected_protocol == "openai"
                         else "anthropic-messages")
        values = discovered_levels
    if args.reasoning:
        values = resolve_reasoning(args.reasoning)
    if not args.name:
        args.name = provider_name_from_url(args.url)
    rule = {
        "providerId": pid,
        "providerName": args.name,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": args.key or "none"},
            "api": {"type": args.api_type, "baseUrl": normalize_base(args.url)},
            "personalModelIds": models,
            "modelOrder": models,
        },
    }
    c = cfg["config"]
    c["providerConfigRules"]["providerRules"].append(rule)
    order = c.setdefault("providerOrder", [])
    if args.position == "top":
        order.insert(0, pid)
    else:
        if pid not in order:
            order.append(pid)
    upsert_model_rules(cfg, pid, models, values)
    tid, written, failed, bak = sync_template_config(
        cfg, rule, args.config, models, values)
    out_json({"ok": True, "providerId": pid, "name": args.name,
              "models": models, "reasoning": values, "templateId": tid,
              "written": written, "failed": failed, "backup": str(bak)})


def cmd_set_models(args):
    cfg = load_config(args.config)
    try:
        rule = find_rule(cfg, args.provider)
    except ValueError as e:
        out_json({"ok": False, "error": str(e)})
        sys.exit(2)
    if not rule:
        out_json({"ok": False, "error": f"找不到供应商: {args.provider}"})
        sys.exit(2)
    pid = rule["providerId"]
    conf = rule["config"]
    old = conf.get("modelOrder", [])
    models = split_models(args.models)
    if args.add:
        models = list(dict.fromkeys(old + models))
    if args.reasoning is None:
        cur = [m for m in cfg["config"]["modelConfigRules"]["providerModelRules"]
               if m.get("providerId") == pid
               and (m.get("config", {}).get("optionSpecs", {}) or {}).get("reasoningLevel")]
        values = (cur[0]["config"]["optionSpecs"]["reasoningLevel"].get("values")
                  if cur else None)
        touch = values is not None
    else:
        values = resolve_reasoning(args.reasoning)
        touch = True
    conf["personalModelIds"] = models
    conf["modelOrder"] = models
    upsert_model_rules(cfg, pid, models, values, prune=args.prune,
                       touch_reasoning=touch)
    bak = backup(args.config)
    save_config(args.config, cfg)
    out_json({"ok": True, "providerId": pid, "name": rule.get("providerName"),
              "models": models, "reasoning": values, "backup": str(bak)})


def cmd_test(args):
    cfg = load_config(args.config)
    try:
        rule = find_rule(cfg, args.provider)
    except ValueError as e:
        out_json({"ok": False, "error": str(e)})
        sys.exit(2)
    if not rule:
        out_json({"ok": False, "error": f"找不到供应商: {args.provider}"})
        sys.exit(2)
    api = rule["config"].get("api", {})
    base = normalize_base(api.get("baseUrl", ""))
    key = rule["config"].get("access", {}).get("apiKey", "")
    typ = api.get("type", "openai-chat-completions")
    timeout = args.timeout
    insecure = args.insecure
    versioned = bool(re.search(r"/v\d+$", base))
    if typ == "anthropic-messages":
        url = base + "/messages" if versioned else base + "/v1/messages"
        body = {"model": args.model, "max_tokens": 64,
                "messages": [{"role": "user", "content": "ping"}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
    else:
        url = base + "/chat/completions" if versioned else base + "/v1/chat/completions"
        body = {"model": args.model, "max_tokens": 64, "stream": False,
                "messages": [{"role": "user", "content": "ping"}]}
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx(insecure)) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        if typ == "anthropic-messages":
            content = "".join(b.get("text", "") for b in resp.get("content", []) if isinstance(b, dict))
        else:
            ch = (resp.get("choices") or [{}])[0]
            content = ch.get("message", {}).get("content", "")
        out_json({"ok": True, "model": args.model, "reply": (content or "")[:120]})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        out_json({"ok": False, "model": args.model, "http": e.code, "detail": detail})
        sys.exit(3)
    except Exception as e:
        out_json({"ok": False, "model": args.model,
                  "error": f"{type(e).__name__}: {e}"})
        sys.exit(3)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help="provider_config.json 路径(默认用户目录)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("providers", help="列出现有供应商")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_providers)

    p = sub.add_parser("models", help="拉取中转站模型列表")
    p.add_argument("--url", required=True)
    p.add_argument("--key", default="")
    p.add_argument("--api-type", default="openai",
                   choices=["openai", "anthropic"])
    p.add_argument("--filter", default="", help="正则过滤模型ID")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("add", help="新增供应商")
    p.add_argument("--name", default="", help="provider name; defaults to URL hostname")
    p.add_argument("--url", required=True)
    p.add_argument("--key", default="none")
    p.add_argument("--api-type", default="openai-chat-completions",
                   choices=["openai-chat-completions", "anthropic-messages"])
    p.add_argument("--models", default="", help="逗号分隔模型ID；留空自动拉取全部模型")
    p.add_argument("--reasoning", default="",
                   help="gpt|gemini|full|none|comma-separated levels; default uses explicit remote capability")
    p.add_argument("--position", default="bottom", choices=["top", "bottom"])
    p.add_argument("--provider-id", default="")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("set-models", help="更新供应商模型与思考强度")
    p.add_argument("--provider", required=True, help="providerId 或名称")
    p.add_argument("--models", required=True, help="逗号分隔模型ID")
    p.add_argument("--reasoning", default=None, help="gpt|gemini|full|none|逗号列表;留空沿用现有")
    p.add_argument("--add", action="store_true", help="在现有列表上追加(默认整体替换)")
    p.add_argument("--prune", action="store_true", help="删除不在列表里的模型配置规则")
    p.set_defaults(fn=cmd_set_models)

    p = sub.add_parser("test", help="最小请求测活模型")
    p.add_argument("--provider", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--timeout", type=int, default=40)
    p.add_argument("--insecure", action="store_true")
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("sync-template", help="把中转站做成内置模板:面板原生弹模型列表+思考强度")
    p.add_argument("--provider", required=True, help="providerId 或名称")
    p.add_argument("--models", default="", help="逗号分隔覆盖;默认拉 /models")
    p.add_argument("--use-config", action="store_true", help="不联网,直接用已存模型列表")
    p.add_argument("--reasoning", default="", help="留空=官方全档(none/low/medium/high/xhigh/max);gpt|gemini|none|逗号列表")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--insecure", action="store_true")
    p.set_defaults(fn=cmd_sync_template)

    args = ap.parse_args()
    args.config = Path(args.config)
    args.fn(args)


if __name__ == "__main__":
    main()
