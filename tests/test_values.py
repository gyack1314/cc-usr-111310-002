"""取值解析：decimal定标拒绝、int64/时间戳边界、base64严格、row_id总序。"""
import base64
import datetime
import unittest

from warehouse_data import encoding
from warehouse_data.schema import Column, Schema
from warehouse_data.values import (INT64_MAX, INT64_MIN, parse_boolean,
                                   parse_bytes, parse_decimal, parse_int64,
                                   parse_timestamp, parse_value)


class ValueTests(unittest.TestCase):
    def setUp(self):
        self.dec = Column({'id': 'd', 'type': 'decimal', 'precision': 6,
                           'scale': 2, 'direction': 'asc', 'nulls': 'last'})

    def test_decimal_exact_scale_equivalent_forms(self):
        self.assertEqual(parse_decimal('1.2', 6, 2), (120, 2))
        self.assertEqual(parse_decimal('1.20', 6, 2), (120, 2))
        self.assertEqual(parse_decimal('-0.00', 6, 2), (0, 2))
        self.assertEqual(parse_decimal('1234.5', 6, 2), (123450, 2))
        self.assertEqual(parse_decimal('100', 6, 0), (100, 0))

    def test_decimal_rounding_rejected(self):
        with self.assertRaises(ValueError):
            parse_decimal('1.235', 6, 2)
        with self.assertRaises(ValueError):
            parse_decimal('0.001', 6, 2)

    def test_decimal_precision_rejected(self):
        with self.assertRaises(ValueError):
            parse_decimal('1234567', 6, 2)
        with self.assertRaises(ValueError):
            parse_decimal('12345.67', 6, 2)

    def test_decimal_lexical_only(self):
        for bad in (1.2, 1, None, '1.2.3', 'NaN', 'Infinity', '1e3', 'abc'):
            with self.assertRaises(ValueError):
                parse_decimal(bad, 6, 2)

    def test_int64_bounds_and_bool_rejection(self):
        self.assertEqual(parse_int64(INT64_MIN), INT64_MIN)
        self.assertEqual(parse_int64(INT64_MAX), INT64_MAX)
        for bad in (INT64_MAX + 1, INT64_MIN - 1, 1.0, True, '1'):
            with self.assertRaises(ValueError):
                parse_int64(bad)

    def test_timestamp_naive_rejected(self):
        with self.assertRaises(ValueError):
            parse_timestamp(datetime.datetime(2026, 1, 1))
        aware = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.assertEqual(parse_timestamp(aware), 1767225600000000)
        self.assertEqual(parse_timestamp(0), 0)
        with self.assertRaises(ValueError):
            parse_timestamp(True)

    def test_bytes_strict_base64(self):
        self.assertEqual(parse_bytes('AAEA'), b'\x00\x01\x00')
        self.assertEqual(parse_bytes(''), b'')
        for bad in ('!@#', 'A', 'AA=A', 12):
            with self.assertRaises(ValueError):
                parse_bytes(bad)

    def test_boolean_strict(self):
        self.assertIs(parse_boolean(True), True)
        for bad in (0, 1, 'true', None):
            with self.assertRaises(ValueError):
                parse_boolean(bad)

    def test_null_passthrough(self):
        self.assertIsNone(parse_value(self.dec, None))


class RowIdTests(unittest.TestCase):
    def test_entry_split_and_total_order(self):
        schema = Schema([{'id': 'f', 'type': 'string',
                          'direction': 'asc', 'nulls': 'last'}])
        vals = ('A',)
        k1 = encoding.encode_entry_key(schema, vals, 'r1')
        k2 = encoding.encode_entry_key(schema, vals, 'r10')
        k3 = encoding.encode_entry_key(schema, vals, 'r2')
        self.assertLess(k1, k2)
        self.assertLess(k2, k3)  # row_id按UTF8字节序：r10<r2，非十进制数值
        for key, rid in ((k1, 'r1'), (k2, 'r10'), (k3, 'r2')):
            v, back = encoding.split_entry_key(schema, key)
            self.assertEqual((v, back), (vals, rid))

    def test_row_id_suffix_keeps_business_bounds(self):
        schema = Schema([{'id': 'f', 'type': 'string',
                          'direction': 'asc', 'nulls': 'last'}])
        business = encoding.encode_tuple(schema, ('A',))
        for rid in ('r1', 'r2', 'zzz'):
            key = encoding.encode_entry_key(schema, ('A',), rid)
            self.assertTrue(key.startswith(business))
        # 其他业务键绝不落在该前缀内
        other = encoding.encode_entry_key(schema, ('Aa',), 'r0')
        self.assertFalse(other.startswith(business))


if __name__ == '__main__':
    unittest.main()
