"""Cross-host checkout metadata must never inspect another host's filesystem."""

from unittest.mock import patch

import pytest

from coordfs.errors import CoordError
from coordfs.checkout_claims import checkout_claim, claim_key, validate_claim


@pytest.mark.parametrize('raw,style,path', [
    ('D:/Repo/Work', 'windows', 'd:\\repo\\work'),
    ('D:\\Repo\\Work\\', 'windows', 'd:\\repo\\work'),
    ('\\\\server\\share\\Repo', 'windows', '\\\\server\\share\\repo'),
    ('/srv/hermes/Repo/', 'posix', '/srv/hermes/Repo'),
    ('/srv//hermes/./Repo', 'posix', '/srv/hermes/Repo'),
    ('/srv/NUL.txt', 'posix', '/srv/NUL.txt'),
])
def test_cross_host_paths_are_lexical_and_keep_their_own_style(raw, style, path):
    with patch('pathlib.Path.resolve', side_effect=AssertionError('foreign resolve')), \
         patch('pathlib.Path.is_dir', side_effect=AssertionError('foreign is_dir')), \
         patch('pathlib.Path.lstat', side_effect=AssertionError('foreign lstat')):
        result = checkout_claim(raw, host='remote-host')
        assert result == {'host': 'remote-host', 'path_style': style, 'path': path}
        assert validate_claim(result) == result


@pytest.mark.parametrize('raw', [
    'relative/repo', 'D:repo', 'D:/repo/../private', '/srv/../private',
    'data:repo', 'scope:repo', 'D:/NUL.txt/repo', 'D:/COM1/repo',
    '\\\\?\\C:\\repo', '\\\\.\\COM1', 'D:/repo:stream', 'D:/repo./x',
    'D:/repo /x', '/srv/\x00repo', '/srv/\nrepo', '/srv/repo\\other',
    'D:/repo/*', '\\\\server', '\\\\server\\NUL', '', '/'+('x'*4096),
])
def test_unsafe_or_ambiguous_claims_are_refused(raw):
    with pytest.raises(CoordError) as error:
        checkout_claim(raw, host='remote-host')
    assert error.value.code == 'invalid_checkout'


@pytest.mark.parametrize('host', ['', 'HOST', '../host', 'host with spaces', None])
def test_host_must_already_be_a_canonical_identity_component(host):
    with pytest.raises(CoordError) as error:
        checkout_claim('/srv/repo', host=host)
    assert error.value.code == 'invalid_checkout_host'


def test_same_path_on_different_hosts_is_not_the_same_binding():
    a = checkout_claim('/srv/repo', host='host-a')
    b = checkout_claim('/srv/repo', host='host-b')
    assert claim_key(a) != claim_key(b)
    assert claim_key(a) == ('host-a', 'posix', '/srv/repo')


def test_case_semantics_belong_to_path_style():
    assert claim_key(checkout_claim('D:/Repo', host='a')) == claim_key(checkout_claim('d:/repo', host='a'))
    assert claim_key(checkout_claim('/srv/Repo', host='a')) != claim_key(checkout_claim('/srv/repo', host='a'))


def test_unicode_is_normalized_without_touching_foreign_storage():
    assert checkout_claim('/srv/cafe\u0301', host='a')['path'] == '/srv/caf\u00e9'


def test_stored_claim_is_closed_canonical_data_not_permission_or_a_command():
    canonical = checkout_claim('D:/Repo', host='a')
    for malformed in [
        {**canonical, 'owner_confirm': True},
        {**canonical, 'host': 'A'},
        {**canonical, 'path': 'D:/Repo'},
        {**canonical, 'path_style': 'posix'},
        {**canonical, 'path_style': 'shell'},
        {'path': '/srv/repo'}, None,
    ]:
        with pytest.raises(CoordError):
            validate_claim(malformed)


def test_explicit_style_disambiguates_posix_double_slash_from_unc():
    assert checkout_claim('//srv/repo', host='a', path_style='posix')['path'] == '/srv/repo'
    assert checkout_claim('//server/share/repo', host='a', path_style='windows')['path'] == '\\\\server\\share\\repo'
