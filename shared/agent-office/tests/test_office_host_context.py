"""The Office facade derives path ownership from its connection identity."""

import os
from unittest.mock import Mock, patch

import pytest

from coordfs.office import Office
from coordfs.checkout_claims import local_host
from coordfs.identity import agent_identity
from coordfs.projects import ProjectRegistry


def remote_office(tmp_path, host='linux-a'):
    return Office(home=tmp_path / 'coord', harness='hermes', host=host, session_id='session-one',
                  transport_principal='hermes')


def test_foreign_presence_keeps_posix_path_and_never_looks_for_server_repo(tmp_path):
    office = remote_office(tmp_path)
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.office.repo_id', side_effect=AssertionError('foreign repo read')):
        result = office.presence(status='idle', workspace='/srv/hermes/Repo')
    assert result['status'] == 'ok', result
    assert result['meta']['host'] == 'linux-a'
    assert result['meta']['workspace'] == '/srv/hermes/Repo'
    assert 'repo' not in result['meta']


@pytest.mark.parametrize('operation', ['presence', 'who', 'project_register', 'work_register_task'])
def test_remote_transport_with_server_host_label_never_gains_server_filesystem_access(tmp_path, operation):
    office = remote_office(tmp_path, host='ailab')
    claimed_path = str(tmp_path / 'remote-checkout')
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.work.detected_local_host', return_value='ailab'), \
         patch('coordfs.office.canonicalize_path', side_effect=AssertionError('remote native path read')), \
         patch('coordfs.office.repo_id', side_effect=AssertionError('remote server repo read')), \
         patch('coordfs.projects.canonicalize_checkout', side_effect=AssertionError('remote project path read')), \
         patch('coordfs.work._checkout', side_effect=AssertionError('remote work path read')):
        if operation == 'project_register':
            result = office.project_register('remote-project', claimed_path)
        elif operation == 'work_register_task':
            result = office.work_register_task(project='remote-project', task_id='one',
                                              checkout=claimed_path, scopes=['src/a.py'],
                                              base_sha='a' * 40, commit_intent='candidate')
        else:
            result = getattr(office, operation)(workspace=claimed_path)
    assert result['status'] == 'ok', result


def test_remote_matching_server_host_never_adopts_hostless_legacy_presence(tmp_path):
    office = remote_office(tmp_path, host='ailab')
    claimed_path = str(tmp_path / 'remote-checkout')
    claimed = office._board_or_raise().claim(
        'agent:legacy-local', 'legacy-local', ttl_s=60, kind='agent',
        meta={'kind': 'presence', 'workspace': claimed_path},
    )
    assert claimed['status'] == 'ok', claimed
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'):
        result = office.who(workspace=claimed_path)
    assert result['status'] == 'ok', result
    assert result['online'] == []


def test_remote_matching_server_host_keeps_registry_read_and_message_lexical(tmp_path):
    office = remote_office(tmp_path, host='ailab')
    checkout = str(tmp_path / 'remote-checkout')
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.projects.canonicalize_checkout', side_effect=AssertionError('server checkout read')):
        registered = office.project_register('remote-project', checkout)
        assert registered['status'] == 'ok', registered
        resolved = office.project_resolve('remote-project')
        assert resolved['status'] == 'ok', resolved
        sent = office.project_send(
            'remote-project', 'task', 'thread', 'run',
            {'harness': 'hermes', 'host': 'ailab', 'session_id': 'peer'}, 'synthetic message',
        )
    assert sent['status'] == 'ok', sent


def test_remote_matching_server_host_reads_persisted_legacy_registry_lexically(tmp_path):
    home = tmp_path / 'coord'
    ProjectRegistry(home=home, local_host='ailab').register('legacy', str(tmp_path / 'checkout'))
    office = remote_office(tmp_path, host='ailab')
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.projects.canonicalize_checkout', side_effect=AssertionError('server checkout read')):
        resolved = office.project_resolve('legacy')
    assert resolved['status'] == 'ok', resolved


def test_remote_matching_server_host_keeps_work_update_and_list_lexical(tmp_path):
    office = remote_office(tmp_path, host='ailab')
    checkout = str(tmp_path / 'remote-checkout')
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.work.detected_local_host', return_value='ailab'), \
         patch('coordfs.work._checkout', side_effect=AssertionError('server checkout read')):
        registered = office.work_register_task(
            project='remote-project', task_id='task', checkout=checkout,
            scopes=['src/a.py'], base_sha='a' * 40, commit_intent='candidate',
        )
        assert registered['status'] == 'ok', registered
        listed = office.work_list_tasks(project='remote-project', checkout=checkout)
        assert listed['status'] == 'ok', listed
        assert [task['task_id'] for task in listed['tasks']] == ['task']
        updated = office.work_update_task(
            project='remote-project', task_id='task', checkout=checkout, status='ready',
        )
    assert updated['status'] == 'ok', updated


def test_local_host_is_native_identity_component_without_socket_probe():
    expected = agent_identity('office', session_id='server', env={})['host']
    with patch('coordfs.identity.socket.gethostname', side_effect=AssertionError('socket probe')):
        assert local_host() == expected


def test_hermes_native_stdio_keeps_local_directory_validation(tmp_path):
    host = local_host()
    office = Office(home=tmp_path / 'coord', harness='hermes', host=host,
                    session_id='local-stdio', transport_principal=None)
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    announced = office.presence(workspace=str(checkout))
    assert announced['status'] == 'ok', announced
    assert announced['meta']['workspace'] == os.path.normcase(str(checkout.resolve()))
    missing = office.work_register_task(
        project='local', task_id='missing', checkout=str(tmp_path / 'missing'),
        scopes=['src/a.py'], base_sha='a' * 40, commit_intent='candidate',
    )
    assert missing['status'] == 'error', missing


def test_workspace_filter_matches_only_the_same_host_and_style(tmp_path):
    a = remote_office(tmp_path)
    b = remote_office(tmp_path, 'linux-b')
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.office.repo_id', side_effect=AssertionError('foreign repo read')):
        assert a.presence(status='idle', workspace='/srv/hermes/Repo')['status'] == 'ok'
        assert b.presence(status='idle', workspace='/srv/hermes/Repo')['status'] == 'ok'
        result = a.who(workspace='/srv/hermes/Repo')
    assert result['status'] == 'ok', result
    assert [row['agent'] for row in result['online']] == [a.agent_id]
    assert result['online'][0]['relation'] == 'same'


def test_unfiltered_who_preserves_foreign_display_without_filesystem_claims(tmp_path):
    foreign = remote_office(tmp_path)
    local = Office(home=tmp_path / 'coord', agent_id='local-reader')
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.office.repo_id', side_effect=AssertionError('unexpected repo read')):
        assert foreign.presence(status='idle', workspace='/srv/Repo')['status'] == 'ok'
        result = local.who()
    entry = next(row for row in result['online'] if row['agent'] == foreign.agent_id)
    assert entry['untrusted_workspace'] == '/srv/Repo'
    assert entry['host'] == 'linux-a'


def test_remote_workspace_filter_never_adopts_hostless_legacy_presence(tmp_path):
    office = remote_office(tmp_path)
    claimed = office._board_or_raise().claim(
        'agent:legacy-local', 'legacy-local', ttl_s=60, kind='agent',
        meta={'kind': 'presence', 'workspace': '/srv/hermes/Repo'},
    )
    assert claimed['status'] == 'ok', claimed
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.office.repo_id', side_effect=AssertionError('foreign repo read')):
        result = office.who(workspace='/srv/hermes/Repo')
    assert result['status'] == 'ok', result
    assert result['online'] == []


@pytest.mark.parametrize('operation', ['presence', 'who'])
def test_remote_bare_identity_cannot_claim_server_workspace(tmp_path, operation):
    office = Office(home=tmp_path / 'coord', agent_id='unqualified', transport_principal='ws-office')
    result = getattr(office, operation)(workspace='/srv/repo')
    assert result['status'] == 'error'
    assert result['code'] == 'invalid_identity'
    assert not office.home.exists()


def test_project_host_is_derived_from_connection_identity(tmp_path):
    office = remote_office(tmp_path)
    registry = Mock()
    registry.register.return_value = {'project_id': 'shared'}
    with patch.object(office, '_project_registry', return_value=registry):
        result = office.project_register('shared', '/srv/repo')
    assert result['status'] == 'ok'
    registry.register.assert_called_once_with('shared', '/srv/repo', aliases=(), checkout_host='linux-a')


@pytest.mark.parametrize('method,args', [
    ('work_register_task', {'project':'p', 'task_id':'t', 'checkout':'/srv/repo', 'scopes':['src/a.py']}),
    ('work_update_task', {'project':'p', 'task_id':'t', 'checkout':'/srv/repo', 'status':'ready'}),
    ('work_list_tasks', {'checkout':'/srv/repo'}),
])
def test_work_checkout_host_cannot_be_supplied_as_an_untrusted_payload_override(tmp_path, method, args):
    office = remote_office(tmp_path)
    with patch('coordfs.work.WorkBoard') as constructor:
        board = constructor.return_value
        for name in ['register_task', 'update_task', 'list_tasks']:
            getattr(board, name).return_value = {'status':'ok'}
        result = getattr(office, method)(**args)
    assert result['status'] == 'ok'
    target = {'work_register_task':'register_task', 'work_update_task':'update_task', 'work_list_tasks':'list_tasks'}[method]
    assert getattr(board, target).call_args.kwargs['checkout_host'] == 'linux-a'


def test_status_only_work_update_does_not_assert_another_hosts_checkout(tmp_path):
    office = remote_office(tmp_path)
    with patch('coordfs.work.WorkBoard') as constructor:
        constructor.return_value.update_task.return_value = {'status':'ok'}
        assert office.work_update_task(project='p', task_id='t', status='ready')['status'] == 'ok'
    assert 'checkout_host' not in constructor.return_value.update_task.call_args.kwargs


def test_derived_native_identity_and_bare_local_identity_keep_local_presence_behavior(tmp_path):
    office = Office(home=tmp_path / 'coord', agent_id='local-actor')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.office.repo_id', return_value=None):
        result = office.presence(status='idle', workspace=str(checkout))
    assert result['status'] == 'ok'
    assert result['meta']['host'] == 'ailab'

    legacy = office._board_or_raise().claim(
        'agent:legacy-local', 'legacy-local', ttl_s=60, kind='agent',
        meta={'kind': 'presence', 'workspace': str(checkout)},
    )
    assert legacy['status'] == 'ok', legacy
    with patch('coordfs.checkout_claims.local_host', return_value='ailab'), \
         patch('coordfs.office.repo_id', return_value=None):
        listed = office.who(workspace=str(checkout))
    assert listed['status'] == 'ok', listed
    assert 'legacy-local' in [row['agent'] for row in listed['online']]
