"""Explicit, scoped URL changes with a credential-free recovery receipt."""
import json
import re
import sqlite3
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


def connect(path, readonly=True):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"CC Switch 数据库不存在: {path}")
    return sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=10)


def provider_url(settings):
    config = tomllib.loads(settings.get("config", ""))
    name = config.get("model_provider")
    return name, config.get("model_providers", {}).get(name, {}).get("base_url"), config


def inspect(path):
    with connect(path) as db:
        result = []
        for pid, name, raw in db.execute("select id,name,settings_config from providers where app_type='codex'"):
            try:
                provider, url, cfg = provider_url(json.loads(raw))
                # URLs with embedded credentials/query are never displayed.
                parsed = urlsplit(url or "")
                shown = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "") + parsed.path if url else None
                result.append({"id": pid, "name": name, "base_url": shown, "model_provider": provider,
                               "model": cfg.get("model"), "effort": cfg.get("model_reasoning_effort")})
            except (ValueError, TypeError):
                result.append({"id": pid, "name": name, "error": "unrecognized_config"})
        return result


def replace_url(settings, expected, target):
    provider, current, _ = provider_url(settings)
    if current != expected:
        raise ValueError("当前 base_url 与预期不同；拒绝覆盖其他修改")
    text = settings["config"]
    # Touch only the selected provider's table, not URLs in comments/other providers.
    table = re.compile(r'^\s*\[\s*model_providers\.(?:' + re.escape(str(provider)) + r'|"' + re.escape(str(provider)) + r'")\s*\]\s*(?:#.*)?$', re.M)
    found = table.search(text)
    if not found:
        raise ValueError("此 TOML 表写法不支持自动修改，请通过 CC Switch 界面修改")
    next_table = re.search(r'^\s*\[', text[found.end():], re.M)
    end = found.end() + next_table.start() if next_table else len(text)
    section = text[found.end():end]
    field = re.compile(r'^(\s*base_url\s*=\s*)(["\'])([^\r\n]*?)\2', re.M)
    matches = list(field.finditer(section))
    if len(matches) != 1:
        raise ValueError("base_url 无法唯一定位")
    match = matches[0]
    section = section[:match.start()] + match.group(1) + json.dumps(target) + section[match.end():]
    updated = dict(settings, config=text[:found.end()] + section + text[end:])
    if provider_url(updated)[1] != target:
        raise ValueError("TOML 更新校验失败")
    return updated


def change(path, pid, target, receipt_path, apply=False):
    p = urlsplit(target)
    if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost", "::1") or p.username or p.password or p.query or p.fragment:
        raise ValueError("接入地址必须是无凭据的本机 http URL")
    with connect(path, readonly=not apply) as db:
        if apply:
            db.execute("begin immediate")
        row = db.execute("select settings_config from providers where id=? and app_type='codex'", (pid,)).fetchone()
        if not row:
            raise ValueError("未找到指定 Codex 供应商")
        settings = json.loads(row[0])
        old = provider_url(settings)[1]
        if not old or old == target:
            raise ValueError("原地址为空或已接入，拒绝生成不可靠的恢复记录")
        old_url = urlsplit(old)
        if old_url.username or old_url.password or old_url.query or old_url.fragment:
            raise ValueError("原地址含凭据或参数，请手动配置")
        updated = replace_url(settings, old, target)
        receipt = {"database": str(Path(path).expanduser().resolve()), "provider_id": pid, "old_url": old, "new_url": target}
        if apply:
            dest = Path(receipt_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Write before commit: even a process crash leaves the original endpoint recoverable.
            with dest.open("x", encoding="utf-8") as f:
                json.dump(receipt, f, ensure_ascii=False, indent=2)
            db.execute("update providers set settings_config=? where id=? and app_type='codex'", (json.dumps(updated, ensure_ascii=False), pid))
        return receipt


def restore(receipt_path, apply=False):
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    with connect(receipt["database"], readonly=not apply) as db:
        if apply:
            db.execute("begin immediate")
        row = db.execute("select settings_config from providers where id=? and app_type='codex'", (receipt["provider_id"],)).fetchone()
        if not row:
            raise ValueError("供应商已删除，未修改任何配置")
        settings = json.loads(row[0])
        if provider_url(settings)[1] == receipt["old_url"]:
            return {"status": "already_restored", **receipt}
        updated = replace_url(settings, receipt["new_url"], receipt["old_url"])
        if apply:
            db.execute("update providers set settings_config=? where id=? and app_type='codex'", (json.dumps(updated, ensure_ascii=False), receipt["provider_id"]))
        return {"status": "restored" if apply else "preview", **receipt}
