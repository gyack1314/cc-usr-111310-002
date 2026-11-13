"""有序索引门面：绑定数据快照、schema摘要与编码版本；构建/打开/范围查询。"""
import hashlib
import json
import os

from . import encoding
from .predicate import Predicate, compile_intervals, evaluate
from .schema import ENCODING_VERSION, Schema
from .segment import (BlockCache, FilePool, SegmentBuilder, SegmentReader)
from .values import format_value, parse_tuple

MANIFEST_NAME = 'index-manifest.json'
SEGMENT_NAME = 'segment.wdtk'


class IndexError_(Exception):
    """索引绑定失效（源/schema/版本变化）。"""


def source_digest(records):
    """对原始记录（含row_id/key原文/source_position）取摘要，源变即失效。"""
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _check_limits(memory_bytes, max_open_files):
    if type(memory_bytes) is not int or memory_bytes < 1:
        raise ValueError('memory_bytes必须是正整数')
    if type(max_open_files) is not int or max_open_files < 2:
        raise ValueError('max_open_files至少为2')


def build_index(manifest, index_dir):
    """从冻结清单构建外存索引段与manifest，返回IndexReader。"""
    if manifest.get('kind') != 'ordered-key-index':
        raise ValueError('清单kind必须是ordered-key-index')
    schema = Schema(manifest['schema'])
    if manifest.get('encoding_version') != ENCODING_VERSION and \
            manifest.get('encoding_version') != 'candidate-v1':
        # 数据自带版本是资料标签；索引实际编码版本以本库冻结版本写入
        raise ValueError('不被支持的清单编码标签')
    snapshot = manifest['snapshot']
    if not isinstance(snapshot, str) or not snapshot:
        raise ValueError('snapshot必须是非空字符串')
    rows_per_block = manifest['rows_per_block']
    if type(rows_per_block) is not int or rows_per_block < 1:
        raise ValueError('rows_per_block必须>=1')
    memory_bytes = manifest['memory_bytes']
    max_open_files = manifest['max_open_files']
    _check_limits(memory_bytes, max_open_files)

    records = manifest['records']
    if not isinstance(records, list) or not records:
        raise ValueError('records必须是非空数组')
    seen_row_ids = set()
    parsed = []
    for rec in records:
        rid = rec['row_id']
        if not isinstance(rid, str) or not rid or rid in seen_row_ids:
            raise ValueError('row_id缺失或重复')
        seen_row_ids.add(rid)
        position = rec['source_position']
        if type(position) is not int or position < 0:
            raise ValueError('source_position必须是非负整数')
        parsed.append((parse_tuple(schema, rec['key']), rid, position))
    if len({p for p in (r[2] for r in parsed)}) != len(parsed):
        raise ValueError('source_position必须唯一且保留原行位置')

    os.makedirs(index_dir, exist_ok=True)
    seg_path = os.path.join(index_dir, SEGMENT_NAME)
    builder = SegmentBuilder(seg_path, schema, snapshot, rows_per_block,
                             memory_bytes, max_open_files)
    for values, rid, position in parsed:
        builder.add(values, rid, position)
    block_count = builder.finish()

    index_manifest = {
        'encoding_version': ENCODING_VERSION,
        'codec_version': encoding.CODEC_VERSION,
        'schema_summary': schema.summary,
        'schema_digest_sha256': schema.digest_hex,
        'snapshot': snapshot,
        'source_digest_sha256': source_digest(records),
        'segment': SEGMENT_NAME,
        'block_count': block_count,
        'rows': len(parsed),
        'limits': {'memory_bytes': memory_bytes,
                   'max_open_files': max_open_files,
                   'rows_per_block': rows_per_block},
    }
    with open(os.path.join(index_dir, MANIFEST_NAME), 'w', encoding='utf-8') as f:
        json.dump(index_manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    return open_index(index_dir, schema=schema)


def open_index(index_dir, schema=None):
    """打开索引并核对快照/schema摘要/编码版本；任一不匹配即拒绝。"""
    with open(os.path.join(index_dir, MANIFEST_NAME), encoding='utf-8') as f:
        im = json.load(f)
    if im['codec_version'] != encoding.CODEC_VERSION:
        raise IndexError_(
            f"索引编码版本{im['codec_version']!r}已过期，当前{encoding.CODEC_VERSION!r}")
    if schema is None:
        schema = Schema(json.loads(im['schema_summary']))
    elif schema.digest_hex != im['schema_digest_sha256']:
        raise IndexError_('schema变更使旧索引失效')
    limits = im['limits']
    pool = FilePool(limits['max_open_files'])
    cache = BlockCache(limits['memory_bytes'])
    seg_path = os.path.join(index_dir, im['segment'])
    reader = SegmentReader(seg_path, schema, pool, cache)
    if reader.snapshot != im['snapshot']:
        raise IndexError_('段快照与manifest不一致')
    return IndexReader(reader, schema, im, pool, cache)


class IndexReader:
    def __init__(self, segment, schema, manifest, pool, cache):
        self.segment = segment
        self.schema = schema
        self.manifest = manifest
        self.pool = pool
        self.cache = cache

    def close(self):
        self.pool.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def query(self, q):
        """编译区间 -> 块seek扫描 -> 逐元组谓词复核；返回完整证据链。"""
        pred = Predicate.from_manifest(self.schema, q)
        intervals = compile_intervals(self.schema, pred)
        hits = []
        blocks_read = []
        if intervals:
            for values, row_id, position, bi in self.segment.scan(intervals):
                if not evaluate(self.schema, values, pred):
                    raise AssertionError('区间扫描命中了谓词不接受的元组')
                hits.append({
                    'tuple': [format_value(c, v)
                              for c, v in zip(self.schema.columns, values)],
                    'encoded_key': encoding.encode_tuple(self.schema, values).hex(),
                    'row_id': row_id,
                    'source_position': position,
                    'block': bi,
                })
                if not blocks_read or blocks_read[-1] != bi:
                    blocks_read.append(bi)
        return {
            'intervals': [
                {'lower': None if lo is None else lo.hex(),
                 'upper': None if hi is None else hi.hex()}
                for lo, hi in intervals],
            'empty_semantically': intervals == [],
            'blocks_read': blocks_read,
            'row_count': len(hits),
            'hits': hits,
        }

    def scan_all_encoded(self):
        """按总序输出全部（业务键+row_id）编码，供穷举序校验。"""
        intervals = [(None, None)]
        out = []
        for values, row_id, position, bi in self.segment.scan(intervals):
            out.append((encoding.encode_entry_key(self.schema, values, row_id),
                        values, row_id, position, bi))
        return out
