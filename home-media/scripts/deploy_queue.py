"""Hot-reload the native Stash queue tasks without restarting media services."""

from dataclasses import replace

from deploy import ROOT, connect, execute

from home_media.config import Settings
from home_media.stash import Stash
from home_media.tunnel import api_tunnel


def main():
    stage = "/home/vector/.home-media-deploy"
    destination = "/var/lib/home-media/plugins/home-media"
    with connect() as ssh, ssh.open_sftp() as sftp:
        for name in ("queue.js", "home-media.yml"):
            source = str(ROOT / "plugin" / name)
            sftp.put(source, stage + "/" + name)
            execute(ssh, f"sudo -n install -o media-stash -g media-stash -m 644 "
                         f"{stage}/{name} {destination}/{name}")
    settings = replace(Settings.load(str(ROOT / "config.toml")),
                       stash_url="http://127.0.0.1:19998")
    with api_tunnel(settings):
        stash = Stash(settings.stash_url, settings.stash_key)
        try:
            print(stash.query("mutation{reloadPlugins}"))
            print(stash.query(
                'mutation{runPluginOperation(plugin_id:"home-media",args:{mode:"status"})}'
            ))
        finally:
            stash.client.close()


if __name__ == "__main__":
    main()
