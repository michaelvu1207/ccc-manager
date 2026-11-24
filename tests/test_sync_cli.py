import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cccswitcher import cli, machines, remote_install, services, sync_config, sync_hub

try:
    from .fakes import FakeWorld
except ImportError:  # `unittest discover -s tests`
    from fakes import FakeWorld


class Services(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME")}
        os.environ["HOME"] = self.tmp.name
        os.environ.pop("XDG_CONFIG_HOME", None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_launchd_and_systemd_units(self):
        path = services.install("agent", load=False, platform="macos", ccc="/x/ccc")
        self.assertEqual(path.name, "dev.maikyon.ccc.sync-agent.plist")
        text = path.read_text()
        self.assertIn("<string>/x/ccc</string><string>sync</string><string>agent</string>", text)
        self.assertIn("<key>KeepAlive</key><true/>", text)
        path = services.install("hub", load=False, platform="linux", ccc="/x/ccc")
        self.assertEqual(path, Path(self.tmp.name) / ".config/systemd/user/ccc-sync-hub.service")
        self.assertIn("ExecStart=/x/ccc sync hub", path.read_text())
        self.assertEqual(services.load_commands("hub", "linux")[-1],
                         ["systemctl", "--user", "restart", "ccc-sync-hub.service"])
        self.assertTrue(services.uninstall("hub", unload=False, platform="linux"))
        self.assertFalse(services.uninstall("hub", unload=False, platform="linux"))
        with self.assertRaises(ValueError):
            services.render("evil", "linux")
        auto = services.install("autoswitch", load=False, platform="linux", ccc="/x/ccc")
        self.assertEqual(auto.name, "ccc-autoswitch.service")
        self.assertIn("ExecStart=/x/ccc auto claude", auto.read_text())
        self.assertIn("<string>/x/ccc</string><string>auto</string><string>claude</string>",
                      services.render("autoswitch", "macos", "/x/ccc"))
        self.assertTrue(services.unit_path("autoswitch", "macos").name == "dev.maikyon.ccc.autoswitch.plist")


class InstallCommands(unittest.TestCase):
    def test_remote_install_commands(self):
        box = machines.Machine(id="box", name="Box", alias="box")
        wheel = Path("/tmp/ccc_manager-0.20.0-py3-none-any.whl")
        mkdir, scp, install = remote_install.install_commands(box, wheel)
        self.assertEqual(mkdir[-2], "box")
        self.assertEqual(scp[-2:], [str(wheel), "box:.cache/ccc-install/" + wheel.name])
        self.assertIn("BatchMode=yes", scp)
        self.assertIn('uv tool install --force "$HOME/.cache/ccc-install/ccc_manager-0.20.0-py3-none-any.whl"',
                      install[-1])
        with self.assertRaises(ValueError):
            remote_install.install_commands(box, Path("/tmp/x; rm -rf ~.whl"))

    def test_remote_install_runs_with_fakes(self):
        with FakeWorld() as world:
            home = world.add_home("box")
            (home / ".local/bin/uv").write_text(
                '#!/bin/sh\necho "$@" > "$HOME/uv.log"\n'
                'printf "#!/bin/sh\\necho ccc-manager 9\\n" > "$HOME/.local/bin/ccc"\n'
                'chmod +x "$HOME/.local/bin/ccc"\n')
            (home / ".local/bin/uv").chmod(0o755)
            scp = world.root / "fake-scp"
            scp.write_text('#!/bin/sh\nfor last; do :; done\nsrc=""\nfor a; do case "$a" in -o) ;; *) '
                           'prev="$src"; src="$a";; esac; done\n'
                           'dest="${last#*:}"; host="${last%%:*}"\n'
                           f'cp "$prev" "{world.root}/homes/$host/$dest"\n')
            scp.chmod(0o755)
            os.environ["CCC_SCP"] = str(scp)
            try:
                wheel = world.root / "ccc_manager-0.20.0-py3-none-any.whl"
                wheel.write_text("wheel")
                out = remote_install.install(machines.Machine(id="box", name="Box", alias="box"), wheel)
            finally:
                os.environ.pop("CCC_SCP")
            self.assertEqual(out, "ccc-manager 9")
            self.assertTrue((home / ".cache/ccc-install" / wheel.name).exists())
            self.assertIn("tool install --force", (home / "uv.log").read_text())


class SyncCli(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld().__enter__()
        os.environ["CCC_SYNC_FILE"] = str(self.world.root / "sync.json")
        os.environ["CCC_SYNC_HUB_DIR"] = str(self.world.root / "hub")

    def tearDown(self):
        os.environ.pop("CCC_SYNC_FILE", None)
        os.environ.pop("CCC_SYNC_HUB_DIR", None)
        self.world.__exit__(None, None, None)

    def test_init_status_share_once(self):
        self.assertEqual(cli.main(["sync", "init", "--hub-url", "http://127.0.0.1:1", "--machine", "hubbox"]), 0)
        self.assertEqual(stat.S_IMODE(sync_config.config_path().stat().st_mode), 0o600)
        self.assertEqual(cli.main(["sync", "init", "--hub-url", "x", "--machine", "y"]), 2)  # no silent re-key
        config = sync_config.load()
        hub = sync_hub.Hub(sync_hub.HubStore(refresh_fn=lambda v: None), config["secret"], port=0,
                           hub_machine="hubbox", interval=3600)
        hub.start()
        try:
            config["hub_url"] = hub.url
            sync_config.save(config)
            self.assertEqual(cli.main(["sync", "once"]), 0)  # nothing local to share yet
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["sync", "status", "--json"]), 0)
            status = json.loads(buf.getvalue())
            self.assertIn("hubbox", status["hub"]["machines"])
            self.assertNotIn(config["secret"], buf.getvalue())
            self.assertEqual(cli.main(["sync", "secret"]), 0)  # stdout is not a TTY here
        finally:
            hub.stop()

    def test_join_from_stdin(self):
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO("s" * 32 + "\n")
        try:
            self.assertEqual(cli.main(["sync", "join", "--hub-url", "https://hub.ts.net", "--machine", "m",
                                       "--secret-stdin"]), 0)
        finally:
            sys.stdin = old
        self.assertEqual(sync_config.load()["secret"], "s" * 32)
        self.assertEqual(sync_config.load()["role"], "agent")


if __name__ == "__main__":
    unittest.main()
