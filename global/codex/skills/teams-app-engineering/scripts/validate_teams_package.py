"""Bounded, read-only Teams ZIP preflight. Not a Microsoft schema validator."""
import argparse
import json
import re
import stat
import struct
import uuid
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MAX_ARCHIVE = 10 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024
MAX_ENTRY = 4 * 1024 * 1024
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

def validate(path):
    errors = []
    def fail(code):
        if code not in errors:
            errors.append(code)
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('non-JSON number')
    def https(value):
        if not isinstance(value, str): return False
        try:
            parsed = urlsplit(value)
            return (parsed.scheme == 'https' and bool(parsed.hostname)
                    and parsed.username is None and parsed.password is None
                    and not any(c.isspace() for c in value))
        except ValueError:
            return False
    def hostname(value):
        if not isinstance(value, str) or len(value) > 253: return False
        plain = value[2:] if value.startswith('*.') else value
        labels = plain.split('.')
        return (len(labels) >= 2 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', s)
                for s in labels) and '*' not in plain)
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
            fail('ZIP_MISSING_OR_TOO_LARGE')
            return result(errors)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 64 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                fail('ZIP_EXPANSION_LIMIT')
                return result(errors)
            names = [i.filename for i in infos]
            normalized = [n.casefold() for n in names]
            if len(set(normalized)) != len(names): fail('ZIP_DUPLICATE_ENTRY')
            for info in infos:
                name = info.filename
                parts = PurePosixPath(name).parts
                if (not parts or name.startswith('/') or '\\' in name or ':' in name
                        or any(p in {'.', '..'} for p in name.split('/'))
                        or any(ord(c) < 32 for c in name)):
                    fail('ZIP_UNSAFE_PATH')
                if info.file_size > MAX_ENTRY or info.flag_bits & 1:
                    fail('ZIP_ENTRY_LIMIT_OR_ENCRYPTED')
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    fail('ZIP_UNSUPPORTED_COMPRESSION')
                if stat.S_ISLNK(info.external_attr >> 16): fail('ZIP_SYMLINK')
            if errors: return result(errors)
            if 'manifest.json' not in names:
                fail('MANIFEST_NOT_AT_ZIP_ROOT')
                return result(errors)
            if archive.getinfo('manifest.json').file_size > 256 * 1024:
                fail('MANIFEST_SIZE_LIMIT')
                return result(errors)
            manifest = json.loads(archive.read('manifest.json').decode('utf-8-sig'),
                                  object_pairs_hook=pairs, parse_constant=invalid_constant)
            if not isinstance(manifest, dict):
                fail('MANIFEST_NOT_OBJECT')
                return result(errors)
            encoded = json.dumps(manifest)
            if re.search(r'\$\{[^}]*\}|\{\{[^}]*\}\}', encoded):
                fail('UNRESOLVED_TEMPLATE_VALUE')
            try: uuid.UUID(manifest['id'])
            except (ValueError, TypeError, KeyError, AttributeError): fail('APP_ID_INVALID')
            if not isinstance(manifest.get('version'), str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', manifest['version']):
                fail('APP_VERSION_INVALID')
            if not isinstance(manifest.get('manifestVersion'), str) or not re.fullmatch(r'[0-9]+\.[0-9]+', manifest['manifestVersion']):
                fail('MANIFEST_VERSION_INVALID')
            domains = manifest.get('validDomains', [])
            if not isinstance(domains, list) or not all(hostname(d) for d in domains):
                fail('VALID_DOMAINS_NOT_HOSTNAMES')
            icons = manifest.get('icons')
            allowed = {'manifest.json'}
            if not isinstance(icons, dict):
                fail('ICON_REFERENCES_MISSING')
            else:
                icon_fields = [('color', 192), ('outline', 32)]
                if 'color32x32' in icons: icon_fields.append(('color32x32', 32))
                for key, size in icon_fields:
                    name = icons.get(key)
                    if not isinstance(name, str) or name not in names or not name.lower().endswith('.png'):
                        fail('ICON_REFERENCE_INVALID')
                        continue
                    allowed.add(name)
                    header = archive.read(name)[:24]
                    if (len(header) != 24 or header[:8] != PNG_SIGNATURE
                            or header[8:16] != b'\x00\x00\x00\x0dIHDR'):
                        fail('ICON_PNG_HEADER_INVALID')
                    elif struct.unpack('>II', header[16:24]) != (size, size):
                        fail('ICON_DIMENSIONS_INVALID')
            for capability, fields in [('staticTabs', ['contentUrl', 'websiteUrl']),
                                       ('configurableTabs', ['configurationUrl'])]:
                tabs = manifest.get(capability, [])
                if not isinstance(tabs, list) or not all(isinstance(t, dict) for t in tabs):
                    fail('TAB_STRUCTURE_INVALID')
                    continue
                for tab in tabs:
                    for field in fields:
                        if field in tab and not https(tab[field]): fail('TAB_URL_NOT_HTTPS')
            localization = manifest.get('localizationInfo', {})
            if isinstance(localization, dict):
                if isinstance(localization.get('defaultLanguageFile'), str) and localization['defaultLanguageFile'].endswith('.json'):
                    allowed.add(localization['defaultLanguageFile'])
                languages = localization.get('additionalLanguages', [])
                if isinstance(languages, list):
                    allowed.update(x['file'] for x in languages if isinstance(x, dict)
                                   and isinstance(x.get('file'), str) and x['file'].endswith('.json'))
            activity_icons = manifest.get('activities', {}).get('activityIcons', []) if isinstance(manifest.get('activities'), dict) else []
            if isinstance(activity_icons, list):
                allowed.update(x['iconFile'] for x in activity_icons if isinstance(x, dict)
                               and isinstance(x.get('iconFile'), str) and x['iconFile'].endswith('.png'))
            def parent_directory(name):
                return name.endswith('/') and any(p.startswith(name) for p in allowed)
            if any(n not in allowed and not parent_directory(n) for n in names): fail('UNEXPECTED_PACKAGE_ENTRY')
            if any(n not in names for n in allowed): fail('REFERENCED_PACKAGE_ENTRY_MISSING')
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, NotImplementedError,
            UnicodeError, RecursionError, zlib.error, zipfile.BadZipFile, zipfile.LargeZipFile):
        fail('UNREADABLE_ZIP_OR_MANIFEST')
    return result(errors)

def result(errors):
    return {'status': 'REPAIR' if errors else 'PASS_OFFLINE', 'errors': errors,
            'scope': 'common local package checks; not full schema or Teams UAT',
            'network_calls': 0, 'writes_performed': 0}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', help='Local Teams app package ZIP')
    args = parser.parse_args()
    value = validate(args.package)
    print(json.dumps(value, sort_keys=True))
    return 2 if value['errors'] else 0

if __name__ == '__main__':
    raise SystemExit(main())
