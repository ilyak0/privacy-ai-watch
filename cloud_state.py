#!/usr/bin/env python3
"""Persist public news metadata as reviewable JSONL between GitHub runners."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import urllib.parse
from pathlib import Path

import collect


def restore(state_dir: Path, data_dir: Path) -> int:
    source = state_dir / 'items.jsonl'
    if not source.exists():
        return 0
    data_dir.mkdir(parents=True, exist_ok=True)
    db = collect.init_db(data_dir / 'archive.sqlite3')
    count = 0
    with db:
        for line in source.read_text(encoding='utf-8').split('\n'):
            if not line.strip():
                continue
            row = json.loads(line)
            db.execute('INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,?,?)', (
                row['canonical_url'], row['url'], row['title'], '', row['published_utc'],
                row['first_seen_utc'], json.dumps(row['topics']), json.dumps(row['regions']),
                json.dumps(row['provenance'], ensure_ascii=False), row['source_kind']))
            if row['published_utc']:
                aliases = row.get('title_aliases') or [{'hash': collect.title_alias_key(row['title']),
                                                       'day': row['published_utc'][:10]}]
                for alias in aliases:
                    db.execute('INSERT OR IGNORE INTO title_aliases_v2 VALUES (?,?,?)', (
                        alias['hash'], alias['day'], row['canonical_url']))
            count += 1
    db.close()
    return count


def export(database: Path, state_dir: Path) -> int:
    db = sqlite3.connect(f'file:{database.resolve()}?mode=ro', uri=True)
    rows = db.execute('SELECT canonical_url,original_url,title,published_utc,first_seen_utc,'
                      'topics_json,regions_json,provenance_json,source_kind '
                      'FROM items ORDER BY canonical_url').fetchall()
    aliases_by_url = {}
    for key, day, url in db.execute('SELECT title_key,published_day,canonical_url FROM title_aliases_v2'):
        alias_hash = key if re.fullmatch(r'[0-9a-f]{64}', key) else hashlib.sha256(key.encode()).hexdigest()
        aliases_by_url.setdefault(url, []).append({'hash': alias_hash, 'day': day})
    db.close()
    lines = []
    source_urls = {s['id']: s['url'] for s in collect.load_config(collect.DEFAULT_CONFIG)['sources']}
    for row in rows:
        # Excerpts are used transiently for matching, never committed or published.
        title = row[2][:180] + ('…' if len(row[2]) > 180 else '')
        provenance = json.loads(row[7])
        base = next((source_urls[k] for k in provenance if k in source_urls), '')
        url = urllib.parse.urljoin(base, row[1])
        for source_id, meta in provenance.items():
            for observation in meta.get('observations', []):
                observation['url'] = urllib.parse.urljoin(source_urls.get(source_id, base), observation['url'])
        item = dict(canonical_url=collect.canonical_url(urllib.parse.urljoin(base, row[0])), url=url, title=title, published_utc=row[3],
                    first_seen_utc=row[4], topics=json.loads(row[5]), regions=json.loads(row[6]),
                    provenance=provenance, source_kind=row[8],
                    title_aliases=sorted(aliases_by_url.get(row[0], []), key=lambda a: (a['day'], a['hash'])))
        lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    collect.atomic_write(state_dir / 'items.jsonl', '\n'.join(lines) + ('\n' if lines else ''))
    return len(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['restore', 'export'])
    parser.add_argument('--state-dir', type=Path, default=Path('state'))
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--database', type=Path)
    args = parser.parse_args()
    count = (restore(args.state_dir, args.data_dir) if args.action == 'restore'
             else export(args.database or args.data_dir / 'archive.sqlite3', args.state_dir))
    print(f'{args.action}: {count} public metadata records')


if __name__ == '__main__':
    main()
