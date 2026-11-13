"""命令行：inspect（旧摘要）、build、query、demo。"""
import argparse
import json
from pathlib import Path

from . import demo as demo_mod
from .domain import summarize
from .index import build_index, open_index
from .io import load


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog='warehouse_data')
    sub = p.add_subparsers(dest='command', required=True)

    sub.add_parser('inspect').add_argument('file')

    pb = sub.add_parser('build', help='从清单构建外存有序索引')
    pb.add_argument('file')
    pb.add_argument('index_dir')

    pq = sub.add_parser('query', help='打开索引执行清单内query')
    pq.add_argument('file')
    pq.add_argument('index_dir')

    sub.add_parser('demo', help='内置端到端演示（编码、区间、块相交）')
    a = p.parse_args(argv)

    if a.command == 'inspect':
        v, digest = load(a.file)
        _print({'manifest_sha256': digest,
                'summary': summarize(v, Path(a.file).parent)})
        return
    if a.command == 'demo':
        report, _ = demo_mod.run_demo()
        _print({'demo': report, 'bulk': demo_mod.block_intersection_demo()})
        return

    v, digest = load(a.file)
    if a.command == 'build':
        reader = build_index(v, a.index_dir)
        _print({'manifest_sha256': digest,
                'index_manifest': reader.manifest,
                'index_dir': a.index_dir})
        reader.close()
        return
    if a.command == 'query':
        with open_index(a.index_dir) as reader:
            _print(reader.query(v['query']))
        return


if __name__ == '__main__':
    main()
