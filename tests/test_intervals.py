"""区间编译 vs 独立逐元组谓词：穷举查询，两者命中集合必须完全一致。"""
import base64
import itertools
import unittest

from warehouse_data import encoding
from warehouse_data.predicate import Predicate, compile_intervals, evaluate
from warehouse_data.schema import Schema
from warehouse_data.values import parse_tuple

SCHEMA = Schema([
    {'id': 'f', 'type': 'string', 'direction': 'asc', 'nulls': 'last'},
    {'id': 'n', 'type': 'int64', 'direction': 'desc', 'nulls': 'first'},
    {'id': 'd', 'type': 'decimal', 'precision': 6, 'scale': 2,
     'direction': 'desc', 'nulls': 'first'},
    {'id': 'b', 'type': 'bytes', 'direction': 'asc', 'nulls': 'last'},
])

FA = [None, 'A', 'B', 'AB']
NB = [None, -10, 0, 10]
DC = [None, '-1.50', '0.00', '2.00', '2.0']
BL = [None, b'', b'\x00', b'a', b'a\x00b', b'ab']


def _raw(f, n, d, b):
    return [f, n, d, None if b is None else base64.b64encode(b).decode()]


def build_domain():
    out = []
    for f, n, d, b in itertools.product(FA, NB, DC, BL):
        out.append(parse_tuple(SCHEMA, _raw(f, n, d, b)))
    return out


DOMAIN = build_domain()


def interval_members(intervals, tuples, schema=SCHEMA):
    """用编译出的字节区间直接判定，模拟纯字节扫描。"""
    hits = set()
    for t in tuples:
        key = encoding.encode_tuple(schema, t)
        for lo, hi in intervals:
            if (lo is None or key >= lo) and (hi is None or key < hi):
                hits.add(t)
                break
    return hits


def predicate_members(pred, tuples, schema=SCHEMA):
    return {t for t in tuples if evaluate(schema, t, pred)}


class IntervalCrossCheckTests(unittest.TestCase):
    def test_range_queries_asc_and_desc(self):
        queries = []
        queries.append({'equal_prefix': ['A'],
                        'range': {'lower': -10, 'lower_closed': True,
                                  'upper': 10, 'upper_closed': False}})
        queries.append({'equal_prefix': ['A'],
                        'range': {'lower': -10, 'lower_closed': True,
                                  'upper': 10, 'upper_closed': True}})
        queries.append({'equal_prefix': ['A'],
                        'range': {'lower': 0, 'lower_closed': False,
                                  'upper': 0, 'upper_closed': False}})
        # desc int列的开/闭
        queries.append({'equal_prefix': ['B'],
                        'range': {'lower': -10, 'lower_closed': False,
                                  'upper': 10, 'upper_closed': False}})
        # desc decimal列（在两列相等前缀之后）
        queries.append({'equal_prefix': ['A', 10],
                        'range': {'lower': '-1.50', 'lower_closed': True,
                                  'upper': '2.00', 'upper_closed': True}})
        queries.append({'equal_prefix': ['A', 10],
                        'range': {'lower': '0.00', 'lower_closed': True,
                                  'upper': '0.00', 'upper_closed': True}})
        queries.append({'equal_prefix': ['A', 10],
                        'range': {'lower': '9.00', 'lower_closed': True,
                                  'upper': '1.00', 'upper_closed': True}})
        for q in queries:
            with self.subTest(q=q):
                pred = Predicate.from_manifest(SCHEMA, q)
                intervals = compile_intervals(SCHEMA, pred)
                self.assertEqual(interval_members(intervals, DOMAIN),
                                 predicate_members(pred, DOMAIN))

    def test_starts_with_queries(self):
        prefix3 = ['A', 0, '0.00']  # 三列精确相等后，第4列才是bytes
        queries = [
            {'equal_prefix': prefix3, 'starts_with': {'base64': ''}},
            {'equal_prefix': prefix3,
             'starts_with': {'base64': 'AA=='}},       # b'a'
            {'equal_prefix': prefix3,
             'starts_with': {'base64': base64.b64encode(b'a\x00').decode()}},
            {'equal_prefix': prefix3,
             'starts_with': {'base64': base64.b64encode(b'ab').decode()}},
            {'equal_prefix': prefix3,
             'starts_with': {'base64': base64.b64encode(b'zz').decode()}},
        ]
        for q in queries:
            with self.subTest(q=q):
                pred = Predicate.from_manifest(SCHEMA, q)
                intervals = compile_intervals(SCHEMA, pred)
                self.assertEqual(interval_members(intervals, DOMAIN),
                                 predicate_members(pred, DOMAIN))

    def test_null_equal_prefix(self):
        q = {'equal_prefix': ['A', None]}
        pred = Predicate.from_manifest(SCHEMA, q)
        intervals = compile_intervals(SCHEMA, pred)
        self.assertEqual(interval_members(intervals, DOMAIN),
                         predicate_members(pred, DOMAIN))
        self.assertTrue(predicate_members(pred, DOMAIN))
        # null不能作为range界
        with self.assertRaises(ValueError):
            Predicate.from_manifest(SCHEMA, {
                'equal_prefix': ['A'],
                'range': {'lower': None, 'lower_closed': True,
                          'upper': '1', 'upper_closed': True}})

    def test_empty_semantic_interval(self):
        q = {'equal_prefix': ['A', 0],
             'range': {'lower': '9', 'lower_closed': True,
                       'upper': '1', 'upper_closed': True}}
        pred = Predicate.from_manifest(SCHEMA, q)
        self.assertEqual(compile_intervals(SCHEMA, pred), [])
        self.assertEqual(predicate_members(pred, DOMAIN), set())

    def test_half_open_boundaries_never_leak_adjacent(self):
        # desc列开下界：等于下界的值必须被排除
        q = {'equal_prefix': ['B'],
             'range': {'lower': 0, 'lower_closed': False,
                       'upper': 10, 'upper_closed': True}}
        pred = Predicate.from_manifest(SCHEMA, q)
        members = predicate_members(pred, DOMAIN)
        for t in members:
            self.assertNotEqual(t[1], 0)
        self.assertEqual(interval_members(
            compile_intervals(SCHEMA, pred), DOMAIN), members)

    def test_string_starts_with_desc_column(self):
        schema = Schema([
            {'id': 'f', 'type': 'string', 'direction': 'asc', 'nulls': 'last'},
            {'id': 's', 'type': 'string', 'direction': 'desc', 'nulls': 'last'},
        ])
        domain = [parse_tuple(schema, x) for x in [
            ['A', ''], ['A', 'a'], ['A', 'ab'], ['A', 'ac'],
            ['A', 'b'], ['A', None], ['B', 'a']]]
        q = {'equal_prefix': ['A'], 'starts_with': 'a'}
        pred = Predicate.from_manifest(schema, q)
        intervals = compile_intervals(schema, pred)
        self.assertEqual(interval_members(intervals, domain, schema),
                         predicate_members(pred, domain, schema))
        # 空desc前缀：全部非null
        q2 = {'equal_prefix': ['A'], 'starts_with': ''}
        pred2 = Predicate.from_manifest(schema, q2)
        members = predicate_members(pred2, domain, schema)
        self.assertEqual(len(members), 5)
        self.assertEqual(interval_members(
            compile_intervals(schema, pred2), domain, schema), members)

    def test_invalid_shapes(self):
        with self.assertRaises(ValueError):
            Predicate.from_manifest(SCHEMA, {'equal_prefix': []})
        with self.assertRaises(ValueError):
            Predicate.from_manifest(SCHEMA, {
                'equal_prefix': ['A', 0, '0.00', b''],
                'starts_with': {'base64': ''}})
        with self.assertRaises(ValueError):
            Predicate.from_manifest(SCHEMA, {
                'equal_prefix': ['A'],
                'range': {'lower': '1', 'lower_closed': True,
                          'upper': '2', 'upper_closed': True},
                'starts_with': {'base64': ''}})
        with self.assertRaises(ValueError):
            Predicate.from_manifest(SCHEMA, {
                'equal_prefix': ['A'], 'starts_with': {'base64': 'AA=='}})


if __name__ == '__main__':
    unittest.main()
