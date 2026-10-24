from unittest.mock import Mock

from mythtv_web import cli


def test_main_runs_app_on_required_interface_and_port(monkeypatch):
    app = Mock()
    monkeypatch.setattr(cli, "create_app", lambda: app)

    cli.main()

    app.run.assert_called_once_with(host="0.0.0.0", port=6555)
