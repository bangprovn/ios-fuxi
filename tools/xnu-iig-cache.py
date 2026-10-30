#!/usr/bin/env python3
"""Replay matching upstream IIG-generated source when building with newer Xcode.

Prepare from the original CI source and object tree (any directory above every
OBJROOT; all copies of one generated file must be identical):
  xnu-iig-cache.py --prepare XNU OBJROOT CACHE
Check that CACHE covers exactly XNU's .iig inputs and is intact:
  xnu-iig-cache.py --verify XNU CACHE
Then pass IIG=/absolute/path/to/xnu-iig-cache.py and XNU_IIG_CACHE=CACHE to make.
No kernel ABI is changed. Edited .iig input is refused and must be regenerated
with the matching IIG version instead.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def one(paths, what):
    if not paths:
        raise ValueError(f'no generated {what}')
    if len({digest(p) for p in paths}) != 1:
        raise ValueError(f'conflicting generated {what}')
    return paths[0]


def prepare(source, objects, cache):
    if cache.exists():
        raise ValueError('cache destination already exists')
    entries, files = {}, []
    for path in sorted((source / 'iokit/DriverKit').glob('*.iig')):
        stem = path.stem
        impls = sorted(objects.rglob(stem + '.iig.cpp'))
        impl = one(impls, f'implementation for {path.name}')
        header = one([p.with_name(stem + '.h') for p in impls if p.with_name(stem + '.h').is_file()],
                     f'header for {path.name}')
        entries[path.name] = {'source_sha256': digest(path),
                             'header_sha256': digest(header),
                             'impl_sha256': digest(impl)}
        files += [(impl, stem + '.iig.cpp'), (header, stem + '.h')]
    if not entries:
        raise ValueError('no IIG inputs found')
    cache.mkdir(parents=True)
    for src, name in files:
        shutil.copyfile(src, cache / name)
    (cache / 'manifest.json').write_text(json.dumps(entries, indent=2) + '\n')
    print(f'{cache}: {len(entries)} source/header/implementation pairs')


def verify(source, cache):
    entries = json.loads((cache / 'manifest.json').read_text())
    inputs = {p.name: p for p in (source / 'iokit/DriverKit').glob('*.iig')}
    if sorted(inputs) != sorted(entries):
        raise ValueError(f'inputs {sorted(set(inputs) ^ set(entries))} not matched by the cache')
    versions = set()
    for name, entry in sorted(entries.items()):
        if digest(inputs[name]) != entry['source_sha256']:
            raise ValueError(f'{name}: source changed; regenerate with a matching iig')
        stem = Path(name).stem
        for file, key in ((stem + '.h', 'header_sha256'), (stem + '.iig.cpp', 'impl_sha256')):
            if digest(cache / file) != entry[key]:
                raise ValueError(f'{file}: generated file hash mismatch')
        versions.add((cache / (stem + '.h')).read_text().split('\n', 1)[0].split(' generated')[0].lstrip('/* '))
    print(f'{cache}: {len(entries)} inputs match; {", ".join(sorted(versions))}')


def replay(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('--def', dest='definition', type=Path, required=True)
    ap.add_argument('--header', type=Path, required=True)
    ap.add_argument('--impl', type=Path, required=True)
    args, _ = ap.parse_known_args(argv)
    cache = Path(os.environ['XNU_IIG_CACHE'])
    entries = json.loads((cache / 'manifest.json').read_text())
    entry = entries[args.definition.name]
    if digest(args.definition) != entry['source_sha256']:
        raise ValueError(f'{args.definition}: source changed; cannot replay generated code')
    stem = args.definition.stem
    for src, dst, key in ((cache / (stem + '.h'), args.header, 'header_sha256'),
                          (cache / (stem + '.iig.cpp'), args.impl, 'impl_sha256')):
        if digest(src) != entry[key]:
            raise ValueError(f'{src}: generated file hash mismatch')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    print(f'IIG replay: {args.definition.name}')


def main():
    try:
        if len(sys.argv) == 5 and sys.argv[1] == '--prepare':
            prepare(*(Path(p) for p in sys.argv[2:]))
        elif len(sys.argv) == 4 and sys.argv[1] == '--verify':
            verify(*(Path(p) for p in sys.argv[2:]))
        else:
            replay(sys.argv[1:])
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(f'IIG cache: {error}') from error


if __name__ == '__main__':
    main()
