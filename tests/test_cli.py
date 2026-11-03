from typer.testing import CliRunner

from proofcast import DISCLAIMER, __version__
from proofcast.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_disclaimer() -> None:
    result = runner.invoke(app, ["disclaimer"])
    assert result.exit_code == 0
    assert DISCLAIMER in result.output
