import pytest

import titanpkg
from titanpkg.cli import main


def test_version_string():
    assert titanpkg.__version__ == "0.1.0"


def test_cli_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "titanpkg 0.1.0" in capsys.readouterr().out


def test_cli_help_without_command(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "usage: titanpkg" in out


def test_update_without_repos_reports_usage_error(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "update"]) == 2
    err = capsys.readouterr().err
    assert "no enabled repositories" in err


def test_self_check_command(capsys):
    assert main(["self-check", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out
