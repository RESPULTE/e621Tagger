"""Configure native media-completion sources without restarting services."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath

from .api import request
from .install_server import CONFIG, SOURCE, run, write

CONTAINER = "recursing_ryan-main_app-1"
LIBRARY_ID = 1
PLUGIN_ID = "unmanic_telegram_completion"
API = "http://127.0.0.1:8889/unmanic/api/v2"
PLUGIN_SOURCE = SOURCE / "unmanic-telegram-completion"
SETTINGS_BACKUP = CONFIG / "unmanic-completion-settings-before.json"
LIBRARY_BACKUP = CONFIG / "unmanic-library-plugins-before.json"
NOTIFICATIONS = CONFIG / "notifications.json"
IMMICH_LIBRARIES = Path("/etc/family-cloud/immich-libraries.json")
ACCESS_FILE = Path("/etc/family-cloud/access.json")


def _safe_error(message):
    raise RuntimeError(message) from None


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _safe_error("required JSON configuration is missing or invalid")


def _adult_roots(value):
    if not isinstance(value, list) or not value:
        _safe_error("adult_roots must be a non-empty list")
    roots = []
    for raw in value:
        if not isinstance(raw, str) or not Path(raw).is_absolute() or ".." in Path(raw).parts:
            _safe_error("adult_roots must contain absolute directory paths")
        try:
            root = Path(raw).resolve(strict=True)
        except OSError:
            _safe_error("an adult root is unavailable")
        if not root.is_dir():
            _safe_error("every adult root must be a directory")
        if root in roots:
            _safe_error("adult_roots contains a duplicate directory")
        if any(root.is_relative_to(existing) or existing.is_relative_to(root) for existing in roots):
            _safe_error("adult_roots must not overlap")
        roots.append(root)
    return sorted(roots, key=str)


def _mounts(container=CONTAINER, run_command=run):
    raw = run_command("docker", "inspect", "--format", "{{json .Mounts}}", container)
    try:
        mounts = json.loads(raw)
    except (TypeError, ValueError):
        _safe_error("container mount data is invalid")
    if not isinstance(mounts, list):
        _safe_error("container mount data is invalid")
    return mounts


def _map_adult_roots(roots, mounts):
    bind_mounts = []
    config_source = None
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "bind":
            continue
        source, destination = mount.get("Source"), mount.get("Destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            continue
        try:
            source_path = Path(source).resolve(strict=True)
        except OSError:
            continue
        if destination == "/config":
            config_source = source_path
        bind_mounts.append((source_path, PurePosixPath(destination)))
    if config_source is None or not config_source.is_dir():
        _safe_error("Unmanic config bind mount is unavailable")

    mapped = []
    for root in roots:
        candidates = []
        for source, destination in bind_mounts:
            try:
                relative = root.relative_to(source)
            except ValueError:
                continue
            target = destination.joinpath(*relative.parts)
            candidates.append((len(source.parts), target))
        if not candidates:
            _safe_error("an adult root is not covered by a container bind mount")
        target = max(candidates, key=lambda item: item[0])[1]
        if (not target.is_absolute() or target == PurePosixPath("/library")
                or not target.is_relative_to(PurePosixPath("/library"))):
            _safe_error("adult roots must map to narrow directories below /library")
        mapped.append(target)
    if len(set(mapped)) != len(mapped):
        _safe_error("adult roots map to duplicate container directories")
    return config_source, mapped


def _immich_library():
    document = _read_json(IMMICH_LIBRARIES)
    libraries = document.get("libraries") if isinstance(document, dict) else None
    if isinstance(libraries, dict):
        entries = list(libraries.values())
    elif isinstance(libraries, list):
        entries = libraries
    else:
        _safe_error("Immich library configuration has an unexpected shape")
    matches = [row for row in entries
               if isinstance(row, dict) and row.get("path") == "/family/uploads"]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str) or not matches[0]["id"]:
        _safe_error("the exact Immich uploads library could not be selected")
    return matches[0]["id"]


def _validate_credentials():
    document = _read_json(ACCESS_FILE)
    account = document.get("immich_viewer") if isinstance(document, dict) else None
    if (not isinstance(account, dict) or not isinstance(account.get("username"), str)
            or not account["username"] or not isinstance(account.get("password"), str)
            or not account["password"]):
        _safe_error("read-only Immich credentials are unavailable")


def _api(path, payload):
    return request(f"{API}{path}", payload, timeout=10)


def _plugin_info(api_request=_api):
    info = api_request("/plugins/info", {
        "plugin_id": PLUGIN_ID,
        "library_id": LIBRARY_ID,
        "prefer_local": True,
    })
    if not isinstance(info, dict):
        _safe_error("Unmanic returned invalid plugin metadata")
    if info.get("plugin_id") not in (None, "", PLUGIN_ID):
        _safe_error("Unmanic returned metadata for an unexpected plugin")
    return info


def _plugin_registered(database):
    """Inspect the Unmanic plugin registry through its existing read-only DB."""
    uri = Path(database).as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(plugins)")}
            if "plugin_id" not in columns:
                _safe_error("Unmanic plugin registry schema is unavailable")
            rows = db.execute("SELECT count(*) FROM plugins WHERE plugin_id = ?",
                              (PLUGIN_ID,)).fetchone()
    except sqlite3.Error:
        _safe_error("Unmanic plugin registration state is unavailable")
    if not rows or rows[0] not in (0, 1):
        _safe_error("Unmanic plugin registration state is ambiguous")
    return rows[0] == 1


def _settings_rows(info):
    rows = info.get("settings")
    if info.get("plugin_id") != PLUGIN_ID or not isinstance(rows, list):
        _safe_error("Unmanic completion plugin settings are unavailable")
    required = {"Enable completion feed", "Adult library ID", "Adult media roots"}
    present = [row.get("key") for row in rows if isinstance(row, dict)]
    if any(present.count(key) != 1 for key in required):
        _safe_error("Unmanic completion plugin settings have an unexpected shape")
    return rows


def _set_settings(rows, container_roots):
    wanted = {
        "Enable completion feed": True,
        "Adult library ID": str(LIBRARY_ID),
        "Adult media roots": json.dumps([str(path) for path in container_roots],
                                        separators=(",", ":")),
    }
    result = copy.deepcopy(rows)
    for row in result:
        if row.get("key") in wanted:
            row["value"] = wanted[row["key"]]
    return result


def _backup_once(path, value):
    path = Path(path)
    if path.is_symlink():
        _safe_error("refusing a symlinked private backup")
    if path.exists():
        if not path.is_file():
            _safe_error("private backup destination is not a file")
        return
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", 0o600)


def _plugin_metadata():
    info = _read_json(PLUGIN_SOURCE / "info.json")
    required = ("id", "name", "author", "description", "version", "icon")
    if any(not isinstance(info.get(key), str) for key in required) or info["id"] != PLUGIN_ID:
        _safe_error("bundled completion plugin metadata is invalid")
    return {"plugin_id": info["id"], **{key: info[key] for key in required[1:]}}


def _library_settings(api_request=_api):
    result = api_request("/settings/library/read", {"id": LIBRARY_ID})
    if not isinstance(result, dict):
        _safe_error("Unmanic library settings have an unexpected shape")
    library = result.get("library_config")
    plugins = result.get("plugins")
    enabled = plugins.get("enabled_plugins") if isinstance(plugins, dict) else None
    if (not isinstance(library, dict) or library.get("id") != LIBRARY_ID
            or not isinstance(enabled, list)
            or any(not isinstance(row, dict) or not isinstance(row.get("plugin_id"), str)
                   for row in enabled)):
        _safe_error("Unmanic library plugin list has an unexpected shape")
    return {"library_config": copy.deepcopy(library),
            "plugins": {"enabled_plugins": copy.deepcopy(enabled)}}


def _library_export(api_request=_api):
    result = api_request("/settings/library/export", {"id": LIBRARY_ID})
    plugins = result.get("plugins") if isinstance(result, dict) else None
    enabled = plugins.get("enabled_plugins") if isinstance(plugins, dict) else None
    if (not isinstance(enabled, list)
            or any(not isinstance(row, dict) or not isinstance(row.get("plugin_id"), str)
                   for row in enabled)):
        _safe_error("Unmanic library plugin settings have an unexpected shape")
    if len({row["plugin_id"] for row in enabled}) != len(enabled):
        _safe_error("Unmanic library plugin settings contain duplicates")
    return copy.deepcopy(enabled)


def _library_payload(baseline, exported_plugins):
    payload = copy.deepcopy(baseline)
    enabled = payload["plugins"]["enabled_plugins"]
    exported = {row["plugin_id"]: row for row in exported_plugins}
    for entry in enabled:
        saved = exported.get(entry["plugin_id"])
        if saved is None:
            _safe_error("Unmanic library plugin settings could not be preserved")
        entry["has_config"] = saved.get("has_config", False)
        if entry["has_config"]:
            entry["settings"] = copy.deepcopy(saved.get("settings", {}))
    matches = [row for row in enabled if row.get("plugin_id") == PLUGIN_ID]
    if len(matches) > 1:
        _safe_error("Unmanic library has duplicate completion plugin assignments")
    if not matches:
        entry = _plugin_metadata()
        entry["library_id"] = LIBRARY_ID
        # Its settings are stored by /plugins/settings/update before assignment.
        # Marking it unconfigured prevents library/write from replacing them with {}.
        entry["has_config"] = False
        enabled.append(entry)
    return payload


def _write_plugin(config_source, *, registered, run_command=run):
    plugin_dir = config_source / ".unmanic" / "plugins" / PLUGIN_ID
    parent = plugin_dir.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        _safe_error("Unmanic plugin directory is unsafe")
    if not parent.exists():
        _safe_error("Unmanic plugin directory is unavailable")
    plugin_dir.mkdir(mode=0o755, parents=False, exist_ok=True)
    if plugin_dir.is_symlink() or not plugin_dir.resolve().is_relative_to(config_source):
        _safe_error("Unmanic plugin directory escapes the config bind mount")

    changed = not registered
    for name in ("plugin.py", "info.json"):
        source = PLUGIN_SOURCE / name
        destination = plugin_dir / name
        if destination.is_symlink():
            _safe_error("refusing a symlinked Unmanic plugin file")
        content = source.read_bytes()
        if not destination.exists() or destination.read_bytes() != content:
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o644)
            os.chown(destination, 1000, 1000)
            changed = True
        else:
            if destination.stat().st_uid != 1000 or destination.stat().st_gid != 1000:
                os.chown(destination, 1000, 1000)
            if destination.stat().st_mode & 0o777 != 0o644:
                os.chmod(destination, 0o644)
        os.chown(plugin_dir, 1000, 1000)
    if changed:
        run_command("docker", "exec", "--user", "1000:1000", "--env", "HOME=/config",
                    CONTAINER, "/opt/venv/bin/unmanic", "--manage-plugins", "--reload-plugins")
    return changed


def _register_plugin(run_command=run):
    run_command("docker", "exec", "--user", "1000:1000", "--env", "HOME=/config",
                CONTAINER, "/opt/venv/bin/unmanic", "--manage-plugins", "--create-plugin",
                "--plugin-id", PLUGIN_ID, "--plugin-name", "Adult Video Completion Feed",
                "--plugin-runners", "emit_postprocessor_complete")


def _notifications_config(immich_library, feed, database):
    if NOTIFICATIONS.exists():
        existing = _read_json(NOTIFICATIONS)
        if not isinstance(existing, dict):
            _safe_error("notification configuration has an unexpected shape")
        config = existing
    else:
        config = {}
    config["unmanic"] = {"enabled": True, "feed": str(feed), "database": str(database)}
    config["immich"] = {
        "enabled": True,
        "url": "http://127.0.0.1:2283/api",
        "library": immich_library,
        "credentials": str(ACCESS_FILE),
        "account": "immich_viewer",
    }
    jellyfin = config.get("jellyfin")
    if jellyfin is None:
        jellyfin = {}
    if not isinstance(jellyfin, dict):
        _safe_error("Jellyfin notification settings have an unexpected shape")
    jellyfin.setdefault("enabled", False)
    jellyfin.setdefault("url", "http://127.0.0.1:8097")
    jellyfin.setdefault("credentials", str(CONFIG / "secrets.json"))
    jellyfin.setdefault("libraries", [])
    config["jellyfin"] = jellyfin
    return config


def install(input_data, *, run_command=run, api_request=_api):
    """Install the feed integration and independent media-source settings."""
    if os.geteuid() != 0:
        _safe_error("installer must run as root")
    roots = _adult_roots(input_data.get("adult_roots") if isinstance(input_data, dict) else None)
    _validate_credentials()
    immich_library = _immich_library()
    mounts = _mounts(run_command=run_command)
    config_source, container_roots = _map_adult_roots(roots, mounts)

    for container_root in container_roots:
        try:
            run_command("docker", "exec", "--user", "1000:1000", "--env", "HOME=/config",
                        CONTAINER, "test", "-d", str(container_root))
        except RuntimeError:
            _safe_error("a mapped adult root is not a directory inside Unmanic")

    database = config_source / ".unmanic" / "config" / "unmanic.db"
    if database.is_symlink() or not database.is_file():
        _safe_error("Unmanic database bind path is unavailable")
    feed = config_source / ".unmanic" / "userdata" / PLUGIN_ID / "completion-events.ndjson"
    planned_notifications = _notifications_config(immich_library, feed, database)

    config_dir = Path(CONFIG)
    config_dir.mkdir(parents=True, exist_ok=True)
    if CONFIG.is_symlink() or not CONFIG.is_dir():
        _safe_error("monitor configuration directory is unsafe")

    plugin_dir = config_source / ".unmanic" / "plugins" / PLUGIN_ID
    if plugin_dir.is_symlink():
        _safe_error("Unmanic plugin directory is unsafe")
    registered = _plugin_registered(database)
    if registered != plugin_dir.is_dir():
        _safe_error("Unmanic plugin registry and plugin directory disagree")
    library_before = _library_settings(api_request)
    exported_plugins_before = _library_export(api_request)
    info_before = _plugin_info(api_request) if registered else None
    if registered and info_before.get("plugin_id") != PLUGIN_ID:
        _safe_error("Unmanic completion plugin metadata is unavailable")
    _backup_once(LIBRARY_BACKUP, {
        "library_id": LIBRARY_ID,
        "library_config": library_before["library_config"],
        "enabled_plugins": exported_plugins_before,
    })
    if registered:
        _backup_once(SETTINGS_BACKUP, {
            "plugin_id": PLUGIN_ID,
            "library_id": LIBRARY_ID,
            "settings": _settings_rows(info_before),
        })

    if not registered:
        _register_plugin(run_command)
    _write_plugin(config_source, registered=registered, run_command=run_command)

    info = _plugin_info(api_request)
    settings = _settings_rows(info)
    _backup_once(SETTINGS_BACKUP, {
        "plugin_id": PLUGIN_ID,
        "library_id": LIBRARY_ID,
        "settings": settings,
    })
    desired_settings = _set_settings(settings, container_roots)
    if desired_settings != settings:
        result = api_request("/plugins/settings/update", {
            "plugin_id": PLUGIN_ID,
            "library_id": LIBRARY_ID,
            "settings": desired_settings,
        })
        if isinstance(result, dict) and result.get("success") is False:
            _safe_error("Unmanic rejected completion plugin settings")

    desired_library = _library_payload(library_before, exported_plugins_before)
    was_enabled = any(row.get("plugin_id") == PLUGIN_ID
                      for row in library_before["plugins"]["enabled_plugins"])
    if not was_enabled:
        result = api_request("/settings/library/write", desired_library)
        if isinstance(result, dict) and result.get("success") is False:
            _safe_error("Unmanic rejected the library plugin assignment")
    verified_library = _library_settings(api_request)
    verified_ids = [row["plugin_id"] for row in verified_library["plugins"]["enabled_plugins"]]
    expected_ids = [row["plugin_id"] for row in desired_library["plugins"]["enabled_plugins"]]
    if (set(verified_ids) != set(expected_ids) or len(verified_ids) != len(expected_ids)
            or verified_library["library_config"] != library_before["library_config"]):
        _safe_error("Unmanic completion plugin assignment did not verify")
    exported_plugins_after = _library_export(api_request)
    before_existing = {row["plugin_id"]: row for row in exported_plugins_before}
    after_existing = {row["plugin_id"]: row for row in exported_plugins_after}
    for plugin_id, prior in before_existing.items():
        if plugin_id != PLUGIN_ID and after_existing.get(plugin_id) != prior:
            _safe_error("Unmanic existing plugin settings did not verify")
    verified_settings = _settings_rows(_plugin_info(api_request))
    if verified_settings != desired_settings:
        _safe_error("Unmanic completion plugin settings did not verify")

    write(NOTIFICATIONS, json.dumps(planned_notifications, ensure_ascii=False, indent=2) + "\n", 0o600)
    return {
        "status": "configured",
        "adult_root_count": len(container_roots),
        "unmanic_enabled_entries": sum(row.get("plugin_id") == PLUGIN_ID
                                       for row in verified_library["plugins"]["enabled_plugins"]),
        "immich_upload_library_selected": True,
        "jellyfin_enabled": bool(planned_notifications["jellyfin"].get("enabled")),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        input_data = json.load(sys.stdin)
        result = install(input_data)
    except Exception as error:
        # Do not print command stderr, paths, API payloads, or credential-bearing config.
        print(json.dumps({"status": "failed", "error": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
