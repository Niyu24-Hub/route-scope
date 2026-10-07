import time

import pytest

from route_scope import __version__
from route_scope import runtime


def test_current_running_service_is_reused(tmp_path):
    runtime.atomic_json(tmp_path/'watch-status.json', {'version': __version__, 'state': 'running',
        'run_id': 'current', 'updated_epoch': time.time(), 'dashboard_url': 'http://127.0.0.1:15927'})
    assert runtime.prepare_watch(tmp_path)['action'] == 'reuse'
    assert not (tmp_path/'control.json').exists()


@pytest.mark.parametrize('version', [None, '0.3.2'])
def test_old_version_is_stopped_cooperatively_before_restart(tmp_path, version):
    runtime.atomic_json(tmp_path/'watch-status.json', {'version': version, 'state': 'running',
        'run_id': 'old', 'updated_epoch': time.time()})
    assert runtime.prepare_watch(tmp_path)['action'] == 'restart'
    assert runtime.read_json(tmp_path/'control.json')['stop_run_id'] == 'old'


def test_locked_old_service_times_out_without_starting_duplicate(tmp_path):
    runtime.atomic_json(tmp_path/'watch-status.json', {'state': 'running', 'run_id': 'old'})
    with runtime.InstanceLock(tmp_path/'watch.lock'):
        with pytest.raises(RuntimeError, match='尚未退出'):
            runtime.prepare_watch(tmp_path, timeout=0)


def test_new_directory_starts_normally(tmp_path):
    assert runtime.prepare_watch(tmp_path/'new')['action'] == 'start'
