"""端到端演示：负边界、decimal异写等值、零字节、升降混排、重复键跨块与块相交。"""
import base64
import json
import tempfile
from pathlib import Path

from .index import build_index
from .predicate import Predicate, evaluate
from .schema import ENCODING_VERSION
from .values import format_value, parse_tuple


def _b64(b):
    return base64.b64encode(b).decode('ascii')


def demo_manifest():
    """覆盖全部编码难点的小样本（rows_per_block=1强制重复键跨块）。"""
    return {
        'kind': 'ordered-key-index',
        'schema': [
            {'id': 'plant', 'type': 'string', 'direction': 'asc', 'nulls': 'last'},
            {'id': 'temp_c', 'type': 'int64', 'direction': 'asc', 'nulls': 'last'},
            {'id': 'reading', 'type': 'decimal', 'precision': 8, 'scale': 3,
             'direction': 'desc', 'nulls': 'first'},
            {'id': 'at', 'type': 'timestamp', 'direction': 'asc', 'nulls': 'last'},
            {'id': 'payload', 'type': 'bytes', 'direction': 'asc', 'nulls': 'last'},
            {'id': 'ok', 'type': 'boolean', 'direction': 'asc', 'nulls': 'last'},
        ],
        'records': [
            {'row_id': 'r01', 'source_position': 0, 'key': [
                'P1', -10, '-0.001', -1000, _b64(b'\x00\x00\xff'), True]},
            {'row_id': 'r02', 'source_position': 1, 'key': [
                'P1', -10, '-0.0010', -999, _b64(b''), False]},
            {'row_id': 'r03', 'source_position': 2, 'key': [
                'P1', -10, '1.500', 0, _b64(b'a\x00b'), True]},
            {'row_id': 'r04', 'source_position': 3, 'key': [
                'P1', 7, '1.5', 1000, _b64(b'a\x00\x00c'), False]},
            {'row_id': 'r05', 'source_position': 4, 'key': [
                'P1', 7, None, 1001, _b64(b'z'), True]},
            {'row_id': 'r06', 'source_position': 5, 'key': [
                'P2', -9223372036854775808, '99999.999', 999999,
                _b64(b'\xff\x00\x01'), True]},
            {'row_id': 'r07', 'source_position': 6, 'key': [
                'P2', 9223372036854775807, '-99999.9990', -999999,
                _b64(b'|'), False]},
            {'row_id': 'r08', 'source_position': 7, 'key': [
                'P2', 0, '0.000', 0, _b64(b'\x00'), True]},
            {'row_id': 'r09', 'source_position': 8, 'key': [
                'P1', 7, '1.500', 2000, _b64(b'a\x00b\x00'), True]},
        ],
        'snapshot': 'demo-snapshot-1',
        'encoding_version': ENCODING_VERSION,
        'memory_bytes': 256,
        'max_open_files': 3,
        'rows_per_block': 1,
    }


def _scenarios():
    return [
        {'name': '负数int64闭开区间', 'query': {
            'equal_prefix': ['P1'],
            'range': {'lower': -10, 'lower_closed': True,
                      'upper': 7, 'upper_closed': False}}},
        {'name': 'decimal异写等值（1.5=1.500）闭区间', 'query': {
            'equal_prefix': ['P1', 7],
            'range': {'lower': '1.5', 'lower_closed': True,
                      'upper': '1.500', 'upper_closed': True}}},
        {'name': 'desc decimal负边界等值（-0.001=-0.0010）', 'query': {
            'equal_prefix': ['P1', -10],
            'range': {'lower': '-0.0010', 'lower_closed': True,
                      'upper': '-0.001', 'upper_closed': True}}},
        {'name': 'bytes零字节前缀（4列相等后定位payload）', 'query': {
            'equal_prefix': ['P1', 7, '1.500', 1000],
            'starts_with': {'base64': _b64(b'a\x00')}}},
        {'name': 'bytes空前缀（匹配该前缀下全部非null）', 'query': {
            'equal_prefix': ['P1', -10, '-0.001', -1000],
            'starts_with': {'base64': ''}}},
        {'name': '显式null精确前缀', 'query': {
            'equal_prefix': ['P1', 7, None]}},
        {'name': '语义为空（下界>上界）', 'query': {
            'equal_prefix': ['P1'],
            'range': {'lower': 100, 'lower_closed': True,
                      'upper': 1, 'upper_closed': True}}},
        {'name': 'int64极值边界', 'query': {
            'equal_prefix': ['P2'],
            'range': {'lower': -9223372036854775808, 'lower_closed': True,
                      'upper': 9223372036854775807, 'upper_closed': True}}},
    ]


def run_demo(index_dir=None):
    manifest = demo_manifest()
    created = index_dir is None
    if created:
        index_dir = tempfile.mkdtemp(prefix='wdtk-demo-')
    reader = build_index(manifest, index_dir)
    schema = reader.schema
    report = {'encoding_version': ENCODING_VERSION,
              'schema_summary': schema.summary,
              'index_dir': str(index_dir), 'scenarios': []}
    try:
        ordered = reader.scan_all_encoded()
        report['total_order'] = [
            {'row_id': rid, 'source_position': pos, 'block': bi,
             'entry_key': key.hex(),
             'tuple': [format_value(c, v)
                       for c, v in zip(schema.columns, values)]}
            for key, values, rid, pos, bi in ordered]
        for sc in _scenarios():
            pred = Predicate.from_manifest(schema, sc['query'])
            result = reader.query(sc['query'])
            expected = [
                r for r in manifest['records']
                if evaluate(schema, parse_tuple(schema, r['key']), pred)]
            result['scenario'] = sc['name']
            result['predicate_expected_row_ids'] = [r['row_id'] for r in expected]
            result['matches_predicate_interpreter'] = (
                {h['row_id'] for h in result['hits']}
                == {r['row_id'] for r in expected})
            report['scenarios'].append(result)
    finally:
        if created:
            reader.close()
        else:
            reader.close()
    return report, manifest


def block_intersection_demo():
    """大样本小范围查询：断言只读取相交块，不扫全表。"""
    n, rows_per_block = 4000, 50
    records = [
        {'row_id': f'k{i:05d}', 'source_position': i,
         'key': [f'F{i % 20:02d}', i, f'{(i % 97) / 100:.2f}']}
        for i in range(n)]
    manifest = {
        'kind': 'ordered-key-index',
        'schema': [
            {'id': 'f', 'type': 'string', 'direction': 'asc', 'nulls': 'last'},
            {'id': 'n', 'type': 'int64', 'direction': 'asc', 'nulls': 'last'},
            {'id': 'd', 'type': 'decimal', 'precision': 6, 'scale': 2,
             'direction': 'desc', 'nulls': 'first'},
        ],
        'records': records, 'snapshot': 'bulk-s1',
        'encoding_version': ENCODING_VERSION,
        'memory_bytes': 4096, 'max_open_files': 4,
        'rows_per_block': rows_per_block}
    with tempfile.TemporaryDirectory(prefix='wdtk-bulk-') as d:
        reader = build_index(manifest, Path(d) / 'idx')
        total_blocks = reader.manifest['block_count']
        q = {'equal_prefix': ['F07'],
             'range': {'lower': 100, 'lower_closed': True,
                       'upper': 300, 'upper_closed': False}}
        result = reader.query(q)
        reader.close()
    return {'total_rows': n, 'total_blocks': total_blocks,
            'blocks_read': result['blocks_read'],
            'rows_returned': result['row_count'],
            'only_intersecting_blocks':
                len(result['blocks_read']) < total_blocks and result['row_count'] > 0}


def main():
    report, _ = run_demo()
    bulk = block_intersection_demo()
    print(json.dumps({'demo': report, 'bulk': bulk},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
