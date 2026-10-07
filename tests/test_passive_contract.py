import pytest

from route_scope.cli import parser


def test_no_active_probe_command_is_exposed():
    with pytest.raises(SystemExit):
        parser().parse_args(['probe','--model','anything','--run'])
