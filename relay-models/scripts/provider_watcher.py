import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import relay_cli


POLL_SECONDS = 2
RETRY_SECONDS = 300
APP_CHECK_SECONDS = 15
DEFAULT_CONFIG = relay_cli.DEFAULT_CONFIG


def _fingerprint(rule):
    conf = rule.get("config", {})
    api = conf.get("api", {})
    key = conf.get("access", {}).get("apiKey", "")
    value = "\0".join((
        rule.get("providerId", ""),
        relay_cli.normalize_base(api.get("baseUrl", "")),
        api.get("type", ""),
        key,
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_path():
    data_dir = os.environ.get("ZCODE_PLUGIN_DATA")
    if not data_dir:
        data_dir = (Path.home() / ".zcode" / "cli" / "plugins" / "data"
                    / "relay-models-market" / "relay-models")
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "provider-watcher-state.json"


def _load_state(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"providers": {}}


def _write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _is_refreshable(rule):
    conf = rule.get("config", {})
    api = conf.get("api", {})
    access = conf.get("access", {})
    provider_id = rule.get("providerId", "")
    return (
        conf.get("group") == "standard-personal"
        and access.get("type") == "api-key"
        and api.get("type") in ("openai-chat-completions", "anthropic-messages")
        and bool(relay_cli.normalize_base(api.get("baseUrl", "")))
        and bool(access.get("apiKey"))
        and access.get("apiKey") != "none"
    )


def _update_model_rules(cfg, rule, models, reasoning_levels):
    return relay_cli.sync_provider_model_rules(cfg, rule, models, reasoning_levels)


def refresh_changed_providers(config_path=DEFAULT_CONFIG, state_path=None, now=None):
    config_path = Path(config_path)
    state_path = Path(state_path or _state_path())
    now = time.time() if now is None else now
    state = _load_state(state_path)
    result = {"refreshed": [], "failed": []}

    try:
        initial = relay_cli.load_config(config_path)
    except (OSError, ValueError):
        return result

    rules = initial.get("config", {}).get("providerConfigRules", {}).get("providerRules", [])
    for snapshot in rules:
        if not _is_refreshable(snapshot):
            continue
        provider_id = snapshot["providerId"]
        fingerprint = _fingerprint(snapshot)
        previous_state = state["providers"].get(provider_id, {})
        if previous_state.get("fingerprint") == fingerprint:
            if previous_state.get("status") == "ok":
                continue
            if previous_state.get("retryAt", 0) > now:
                continue

        conf = snapshot["config"]
        api = conf["api"]
        protocol = "anthropic" if api["type"] == "anthropic-messages" else "openai"
        try:
            _endpoint, models, levels, _protocol, _errors = relay_cli.discover_models(
                api["baseUrl"], conf["access"]["apiKey"], protocol, 20, False)
        except Exception:
            models, levels = [], None
        if not models:
            state["providers"][provider_id] = {
                "fingerprint": fingerprint, "status": "failed",
                "retryAt": now + RETRY_SECONDS,
            }
            result["failed"].append(provider_id)
            _write_state(state_path, state)
            continue

        try:
            latest_bytes = config_path.read_bytes()
            latest = json.loads(latest_bytes.decode("utf-8"))
            rule = next((r for r in latest["config"]["providerConfigRules"]["providerRules"]
                         if r.get("providerId") == provider_id), None)
            if not rule or not _is_refreshable(rule) or _fingerprint(rule) != fingerprint:
                continue
            enabled_ids = _update_model_rules(latest, rule, models, levels)
            _tid, written, failed, _backup = relay_cli.sync_template_config(
                latest, rule, config_path, models, levels, enabled_ids=enabled_ids,
                expected_config_hash=hashlib.sha256(latest_bytes).hexdigest())
            if failed:
                raise RuntimeError("some runtime templates could not be updated")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            state["providers"][provider_id] = {
                "fingerprint": fingerprint, "status": "failed",
                "retryAt": now + RETRY_SECONDS,
            }
            result["failed"].append(provider_id)
            _write_state(state_path, state)
            continue

        state["providers"][provider_id] = {
            "fingerprint": fingerprint, "status": "ok", "modelCount": len(models),
            "retryAt": 0,
        }
        result["refreshed"].append(provider_id)
        _write_state(state_path, state)

    return result


def _acquire_singleton_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if os.name == "nt":
        import msvcrt
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            return None
    else:
        import fcntl
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return None
    return stream


def _watch(config_path, state_path):
    lock = _acquire_singleton_lock(state_path.with_suffix(".lock"))
    if lock is None:
        return 0
    try:
        next_app_check = 0
        while True:
            try:
                refresh_changed_providers(config_path, state_path)
            except Exception:
                pass
            if os.name == "nt" and time.monotonic() >= next_app_check:
                if not _zcode_is_running() or not _plugin_is_enabled():
                    return 0
                next_app_check = time.monotonic() + APP_CHECK_SECONDS
            time.sleep(POLL_SECONDS)
    finally:
        lock.close()


def _zcode_is_running():
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq ZCode.exe", "/NH"],
        capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0 and "ZCode.exe" in result.stdout


def _plugin_is_enabled():
    path = Path.home() / ".zcode" / "cli" / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        enabled = cfg.get("plugins", {}).get("enabledPlugins", {})
        return enabled.get("relay-models@relay-models-market", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def _start(config_path, state_path):
    args = [sys.executable, str(Path(__file__).resolve()), "--watch",
            "--config", str(config_path), "--state", str(state_path)]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        options["start_new_session"] = True
    subprocess.Popen(args, **options)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=_state_path())
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.watch:
        return _watch(args.config, args.state)
    if args.start:
        return _start(args.config, args.state)
    parser.error("specify --start or --watch")


if __name__ == "__main__":
    raise SystemExit(main())

