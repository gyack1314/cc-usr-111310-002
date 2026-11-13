"""小样本穷举：编码字节序 == 业务序；严格可逆；转义/截断/尾随拒绝。"""
import base64
import itertools
import unittest

from warehouse_data import encoding
from warehouse_data.schema import Column, Schema
from warehouse_data.values import parse_tuple

SCHEMA = Schema([
    {'id': 'f', 'type': 'string', 'direction': 'asc', 'nulls': 'last'},
    {'id': 'n', 'type': 'int64', 'direction': 'desc', 'nulls': 'first'},
    {'id': 'd', 'type': 'decimal', 'precision': 6, 'scale': 2,
     'direction': 'asc', 'nulls': 'last'},
    {'id': 't', 'type': 'timestamp', 'direction': 'desc', 'nulls': 'last'},
    {'id': 'b', 'type': 'bytes', 'direction': 'asc', 'nulls': 'first'},
    {'id': 'ok', 'type': 'boolean', 'direction': 'asc', 'nulls': 'last'},
])

STRINGS = ['', 'A', 'Aa', 'B', '中', 'a\x00'.replace('\x00', 'x'), 'ab']
INTS = [-9223372036854775808, -100, -1, 0, 1, 100, 9223372036854775807]
DECIMALS = ['-1.20', '-0.01', '0.00', '1.2', '1.20', '9999.99']
TS = [-1000, -1, 0, 1, 1000]
BLOBS = [b'', b'\x00', b'\x00\x00', b'\x01', b'\x00\xff', b'a', b'a\x00',
         b'a\x00b', b'ab', b'\xff']
BOOLS = [False, True]


def business_compare(schema, a, b):
    """按契约的业务比较：null独立定位，desc仅反转非null值。"""
    for col, x, y in zip(schema.columns, a, b):
        if x is None and y is None:
            continue
        if x is None:
            return -1 if col.nulls == 'first' else 1
        if y is None:
            return 1 if col.nulls == 'first' else -1
        c = (x > y) - (x < y)
        if c:
            return -c if col.direction == 'desc' else c
    return 0


class ExhaustiveEncodingTests(unittest.TestCase):
    def setUp(self):
        # 有界抽样（约1500元组）+ 手工边界/null用例，保证O(n^2)配对可在秒级完成
        self.tuples = []
        domains = [
            [None] + STRINGS,
            [None] + INTS,
            [None] + DECIMALS,
            [None] + TS,
            [None] + BLOBS,
            [None] + BOOLS,
        ]
        raw = list(itertools.product(*domains))
        target = 1500
        picked = set(raw[::max(1, len(raw) // target)])
        # 手工边界：每列极值与null分别出现在不同位置，其余列取首值
        firsts = [d[1] for d in domains]
        for ci, d in enumerate(domains):
            for special in (None, d[1], d[-1]):
                combo = list(firsts)
                combo[ci] = special
                picked.add(tuple(combo))
        for combo in sorted(picked, key=lambda c: repr(c)):
            self.tuples.append(parse_tuple(SCHEMA, self._to_raw(combo)))

    @staticmethod
    def _to_raw(combo):
        f, n, d, t, b, ok = combo
        return [f, n, d, t, None if b is None else base64.b64encode(b).decode(),
                ok]

    def test_order_equivalence_all_pairs(self):
        encoded = [(encoding.encode_tuple(SCHEMA, t), t) for t in self.tuples]
        keys = [k for k, _ in encoded]
        self.assertEqual(len(keys), len(set(keys)))  # 业务键各不相同（本样本）
        for (ka, ta), (kb, tb) in itertools.combinations(encoded, 2):
            byte_cmp = (ka > kb) - (ka < kb)
            biz_cmp = business_compare(SCHEMA, ta, tb)
            self.assertEqual(byte_cmp, biz_cmp,
                             msg=f'{ta!r} vs {tb!r}: {ka.hex()} {kb.hex()}')

    def test_sorted_matches_python_business_sort(self):
        tuples = self.tuples
        byte_sorted = sorted(tuples, key=lambda t: encoding.encode_tuple(SCHEMA, t))
        import functools
        biz_sorted = sorted(tuples,
                           key=functools.cmp_to_key(
                               lambda a, b: business_compare(SCHEMA, a, b)))
        self.assertEqual(byte_sorted, biz_sorted)

    def test_roundtrip_strict(self):
        for t in self.tuples:
            data = encoding.encode_tuple(SCHEMA, t)
            decoded, pos = encoding.decode_tuple(SCHEMA, data)
            self.assertEqual(pos, len(data))
            self.assertEqual(decoded, t)

    def test_prefix_property(self):
        for t in self.tuples[:400]:
            full = encoding.encode_tuple(SCHEMA, t)
            for k in range(1, len(SCHEMA)):
                prefix = b''.join(
                    encoding.encode_column(SCHEMA[i], t[i]) for i in range(k))
                self.assertTrue(full.startswith(prefix))

    def test_trailing_bytes_rejected(self):
        t = parse_tuple(SCHEMA, self._to_raw(
            ('A', 1, '0.00', 0, b'x', True)))
        data = encoding.encode_tuple(SCHEMA, t)
        with self.assertRaises(encoding.CodecError):
            encoding.decode_tuple(SCHEMA, data + b'\x99')
        with self.assertRaises(encoding.CodecError):
            encoding.decode_tuple(SCHEMA, data[:-1])

    def test_bad_escape_rejected(self):
        with self.assertRaises(encoding.CodecError):
            encoding._unescape(b'\x01\x02')
        with self.assertRaises(encoding.CodecError):
            encoding._unescape(b'abc\x01')

    def test_missing_field_rejected(self):
        t = parse_tuple(SCHEMA, self._to_raw(
            ('A', 1, '0.00', 0, b'', True)))
        with self.assertRaises(encoding.CodecError):
            encoding.encode_tuple(SCHEMA, t[:-1])

    def test_null_tag_independent_of_direction(self):
        col_desc_first = SCHEMA.columns[1]  # int64 desc nulls first
        col_asc_last = SCHEMA.columns[0]    # string asc nulls last
        self.assertEqual(encoding.encode_column(col_desc_first, None), b'\x00')
        self.assertEqual(encoding.encode_column(col_asc_last, None), b'\x20')
        # desc/nulls-first: null在任何非null值之前；desc/nulls-last反之
        vals = [None, -5, 0, 9]
        enc = sorted((encoding.encode_column(col_desc_first, v), v) for v in vals)
        self.assertIsNone(enc[0][1])
        col_desc_last = Column({'id': 'x', 'type': 'int64', 'direction': 'desc',
                                'nulls': 'last'})
        enc2 = sorted((encoding.encode_column(col_desc_last, v), v) for v in vals)
        self.assertIsNone(enc2[-1][1])
        # 往返一致
        for col in (col_desc_first, col_desc_last):
            back, np = encoding.decode_column(col, encoding.encode_column(col, None), 0)
            self.assertEqual((back, np), (None, 1))

    def test_signed_varint_order_boundaries(self):
        nums = [-2 ** 63, -2 ** 63 + 1, -256, -255, -1, 0, 1, 255, 256,
                2 ** 63 - 1]
        enc = [encoding._signed_varint(n) for n in nums]
        self.assertEqual(sorted(enc), enc)
        for n in nums:
            back, pos = encoding._read_signed_varint(
                encoding._signed_varint(n), 0)
            self.assertEqual((back, pos), (n, len(encoding._signed_varint(n))))

    def test_zero_bytes_and_escapes_roundtrip(self):
        schema = Schema([{'id': 'b', 'type': 'bytes',
                          'direction': 'asc', 'nulls': 'last'}])
        for raw in (b'\x00', b'\x01', b'\x00\x01\x00', b'\xff\x00\xff'):
            v = parse_tuple(schema, [base64.b64encode(raw).decode()])
            data = encoding.encode_tuple(schema, v)
            self.assertEqual(encoding.decode_tuple(schema, data)[0], (raw,))

    def test_successor(self):
        self.assertEqual(encoding.successor(b'\x01\xff'), b'\x02')
        self.assertEqual(encoding.successor(b'ab'), b'ac')
        self.assertIsNone(encoding.successor(b'\xff\xff'))


if __name__ == '__main__':
    unittest.main()
