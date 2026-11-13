"""外存段与索引门面：总序、重复键跨块、资源上限、损坏与失效绑定。"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from warehouse_data import encoding
from warehouse_data.index import IndexError_, build_index, open_index, source_digest
from warehouse_data.io import load
from warehouse_data.predicate import Predicate, compile_intervals, evaluate
from warehouse_data.schema import Schema
from warehouse_data.segment import (ResourceLimitError, SegmentCorruptError,
                                    FilePool)
from warehouse_data.values import format_value, parse_tuple


def _fixture(name):
    v, _ = load(Path('data') / name)
    return v


class FixtureIndexTests(unittest.TestCase):
    def test_normal_fixture_query_and_cross_block_duplicates(self):
        v = _fixture('normal.json')
        with tempfile.TemporaryDirectory() as d:
            idx_dir = Path(d) / 'idx'
            reader = build_index(v, idx_dir)
            self.assertEqual(reader.manifest['block_count'], len(v['records']))
            result = reader.query(v['query'])
            # 谓词解释器期望（清单顺序）
            schema = reader.schema
            pred = Predicate.from_manifest(schema, v['query'])
            expected = {r['row_id'] for r in v['records']
                        if evaluate(schema, parse_tuple(schema, r['key']), pred)}
            self.assertEqual({h['row_id'] for h in result['hits']}, expected)
            # 返回严格按条目总序（业务键+row_id）非降
            enc_keys = [bytes.fromhex(h['encoded_key'])
                        + encoding.encode_row_id(h['row_id'])
                        for h in result['hits']]
            self.assertEqual(enc_keys, sorted(enc_keys))
            # 区间包含命中业务键的全部row_id：-1.20对应r1与r6
            ids = {h['row_id'] for h in result['hits']}
            self.assertIn('r1', ids)
            self.assertIn('r6', ids)
            # 输出证据链字段完整
            h0 = result['hits'][0]
            self.assertEqual(set(h0), {'tuple', 'encoded_key', 'row_id',
                                       'source_position', 'block'})
            self.assertEqual(
                encoding.encode_tuple(schema, parse_tuple(schema,
                    v['records'][h0['source_position']]['key'])).hex(),
                h0['encoded_key'])
            reader.close()

    def test_duplicate_key_cross_block_prefix(self):
        v = _fixture('duplicate-key-cross-block.json')
        with tempfile.TemporaryDirectory() as d:
            reader = build_index(v, Path(d) / 'i')
            result = reader.query(v['query'])
            # r2(1.2)与r4(1.20)业务键相等、row_id不同，rows_per_block=1必跨块
            ids = [h['row_id'] for h in result['hits']]
            self.assertEqual(sorted(ids), ['r2', 'r4'])
            blocks = {h['block'] for h in result['hits']}
            self.assertGreater(len(blocks), 1)
            reader.close()

    def test_null_equal_prefix_and_empty_prefix(self):
        v = _fixture('null-and-empty-prefix.json')
        with tempfile.TemporaryDirectory() as d:
            reader = build_index(v, Path(d) / 'i')
            result = reader.query(v['query'])
            # A/null 精确前缀，bytes空前缀：只命中r3
            self.assertEqual([h['row_id'] for h in result['hits']], ['r3'])
            reader.close()

    def test_invalid_config_rejected(self):
        v = _fixture('invalid-config.json')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                build_index(v, Path(d) / 'i')

    def test_total_order_and_stable_row_id(self):
        v = _fixture('normal.json')
        with tempfile.TemporaryDirectory() as d:
            reader = build_index(v, Path(d) / 'i')
            ordered = reader.scan_all_encoded()
            keys = [row[0] for row in ordered]
            self.assertEqual(keys, sorted(keys))
            # 同业务键r2/r4按row_id升序
            r2 = next(i for i, row in enumerate(ordered) if row[2] == 'r2')
            r4 = next(i for i, row in enumerate(ordered) if row[2] == 'r4')
            self.assertLess(r2, r4)
            reader.close()


class CorruptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.v = _fixture('normal.json')
        self.idx = Path(self.tmp.name) / 'i'
        self.reader = build_index(self.v, self.idx)
        self.reader.close()
        self.seg_path = self.idx / 'segment.wdtk'

    def tearDown(self):
        self.tmp.cleanup()

    def _reopen(self):
        return open_index(self.idx)

    def test_flip_byte_is_corruption_not_empty(self):
        raw = bytearray(self.seg_path.read_bytes())
        # 翻转数据区中部一个字节
        mid = len(raw) // 2
        raw[mid] ^= 0x01
        self.seg_path.write_bytes(bytes(raw))
        with self.assertRaises(SegmentCorruptError):
            self._reopen()

    def test_truncation_rejected(self):
        raw = self.seg_path.read_bytes()
        self.seg_path.write_bytes(raw[:-40])
        with self.assertRaises(SegmentCorruptError):
            self._reopen()

    def test_trailing_bytes_rejected(self):
        with open(self.seg_path, 'ab') as f:
            f.write(b'\x99')
        with self.assertRaises(SegmentCorruptError):
            self._reopen()

    def test_magic_mismatch_rejected(self):
        raw = bytearray(self.seg_path.read_bytes())
        raw[0] ^= 0xFF
        self.seg_path.write_bytes(bytes(raw))
        with self.assertRaises(SegmentCorruptError):
            self._reopen()


class StaleBindingTests(unittest.TestCase):
    def test_schema_change_invalidates(self):
        v = _fixture('normal.json')
        with tempfile.TemporaryDirectory() as d:
            idx = Path(d) / 'i'
            build_index(v, idx).close()
            changed = json.loads(json.dumps(v))
            changed['schema'][0]['direction'] = 'desc'
            new_schema = Schema(changed['schema'])
            with self.assertRaises(IndexError_):
                open_index(idx, schema=new_schema)

    def test_encoding_version_mismatch_rejected(self):
        v = _fixture('normal.json')
        with tempfile.TemporaryDirectory() as d:
            idx = Path(d) / 'i'
            build_index(v, idx).close()
            mp = idx / 'index-manifest.json'
            m = json.loads(mp.read_text(encoding='utf-8'))
            m['codec_version'] = 'typedkey-v9'
            mp.write_text(json.dumps(m), encoding='utf-8')
            with self.assertRaises(IndexError_):
                open_index(idx)

    def test_source_digest_changes(self):
        v = _fixture('normal.json')
        before = source_digest(v['records'])
        changed = json.loads(json.dumps(v))
        changed['records'][0]['key'][0] = 'Z'
        self.assertNotEqual(before, source_digest(changed['records']))


class ResourceLimitTests(unittest.TestCase):
    def test_open_file_cap_enforced(self):
        pool = FilePool(2)
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(5):
                p = Path(d) / f'f{i}'
                p.write_bytes(b'x')
                paths.append(p)
                pool.open(p)
            self.assertLessEqual(len(pool), 2)
            pool.close()

    def test_invalid_limits_rejected(self):
        v = _fixture('normal.json')
        for patch in ({'max_open_files': 1}, {'memory_bytes': 0},
                      {'rows_per_block': 0}):
            bad = json.loads(json.dumps(v))
            bad.update(patch)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises((ValueError, ResourceLimitError)):
                    build_index(bad, Path(d) / 'i')


class BulkSeekTests(unittest.TestCase):
    def test_small_range_reads_only_intersecting_blocks(self):
        n, rpb = 3000, 30
        records = [
            {'row_id': f'k{i:05d}', 'source_position': i,
             'key': [f'F{i % 10:02d}', i, f'{(i % 97) / 100:.2f}']}
            for i in range(n)]
        manifest = {
            'kind': 'ordered-key-index',
            'schema': [
                {'id': 'f', 'type': 'string', 'direction': 'asc',
                 'nulls': 'last'},
                {'id': 'n', 'type': 'int64', 'direction': 'asc',
                 'nulls': 'last'},
                {'id': 'd', 'type': 'decimal', 'precision': 6, 'scale': 2,
                 'direction': 'desc', 'nulls': 'first'}],
            'records': records, 'snapshot': 'bulk-s1',
            'encoding_version': 'typedkey-v1',
            'memory_bytes': 2048, 'max_open_files': 4,
            'rows_per_block': rpb}
        with tempfile.TemporaryDirectory() as d:
            reader = build_index(manifest, Path(d) / 'idx')
            total_blocks = reader.manifest['block_count']
            q = {'equal_prefix': ['F03'],
                 'range': {'lower': 100, 'lower_closed': True,
                           'upper': 400, 'upper_closed': False}}
            result = reader.query(q)
            pred = Predicate.from_manifest(reader.schema, q)
            expected = sum(evaluate(reader.schema,
                                    parse_tuple(reader.schema, r['key']), pred)
                           for r in records)
            self.assertEqual(result['row_count'], expected)
            self.assertLess(len(result['blocks_read']), total_blocks)
            # 相交块连续且数量远小于总块数
            self.assertEqual(result['blocks_read'],
                             list(range(min(result['blocks_read']),
                                        max(result['blocks_read']) + 1)))
            self.assertLessEqual(len(result['blocks_read']),
                                (expected // rpb) + 2)
            reader.close()


if __name__ == '__main__':
    unittest.main()
