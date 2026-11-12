"""What the packaged executable needs from the source tree: a working folder chooser and a safe launcher."""
import sys

from meloshelf.application import organize_api


def test_the_folder_chooser_runs_through_the_launcher_flag_when_packaged(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    command = organize_api.picker_command('Choose')
    assert command[0] == sys.executable and command[1:] == ['--pick-folder', 'Choose'], 'never re-run the whole app with -c'


def test_the_folder_chooser_uses_an_inline_script_from_source(monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    command = organize_api.picker_command('Choose')
    assert command[:2] == [sys.executable, '-c'] and 'askdirectory' in command[2]


def test_a_second_launch_reuses_a_running_instance_only_when_it_answers_on_loopback(tmp_path):
    import json
    from meloshelf.launcher import running_instance
    state = tmp_path / 'instance.json'
    assert running_instance(state, probe=lambda u: True) == '', 'no file: nothing running'
    state.write_text(json.dumps({'url': 'http://127.0.0.1:5555/'}), encoding='utf-8')
    assert running_instance(state, probe=lambda u: True) == 'http://127.0.0.1:5555/'
    assert running_instance(state, probe=lambda u: False) == '', 'a stale file from a crashed run is ignored'
    state.write_text(json.dumps({'url': 'http://evil.example/'}), encoding='utf-8')
    assert running_instance(state, probe=lambda u: True) == '', 'only a loopback address is ever opened'
    state.write_text('not json', encoding='utf-8')
    assert running_instance(state, probe=lambda u: True) == ''


def test_the_launcher_keeps_its_files_outside_the_installation_folder(tmp_path):
    from meloshelf.launcher import data_dir
    assert data_dir({'LOCALAPPDATA': str(tmp_path)}) == tmp_path / 'MeloShelf'
