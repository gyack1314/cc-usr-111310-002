"""清单原文 -> 有类型Python值；显式null单独保留，缺字段在编码层拒绝。"""
import base64
import binascii
import datetime
import decimal
import re

_DECIMAL_RE = re.compile(r'^[+-]?\d+(?:\.\d+)?$')
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1


class ValueError_(ValueError):
    """取值无法按列类型解析。"""


def parse_int64(x):
    if type(x) is bool or type(x) is not int:
        raise ValueError_('int64需要JSON整数')
    if not INT64_MIN <= x <= INT64_MAX:
        raise ValueError_('int64超出有符号64位范围')
    return x


def parse_decimal(x, precision, scale):
    """按声明scale精确转换为有限位整数（unscaled, scale）；需舍入即拒绝。"""
    if not isinstance(x, str) or not _DECIMAL_RE.match(x):
        raise ValueError_('decimal必须是十进制词法字符串')
    try:
        d = decimal.Decimal(x)
    except decimal.InvalidOperation:
        raise ValueError_('decimal无法解析')
    if not d.is_finite():
        raise ValueError_('decimal必须有限')
    if d.as_tuple().exponent > 0 and len(d.as_tuple().digits) > precision:
        raise ValueError_('decimal超出precision')
    target = decimal.Decimal(1).scaleb(-scale)
    try:
        q = d.quantize(target, context=decimal.Context(
            prec=40, rounding=decimal.ROUND_HALF_UP))
        if q != d:
            raise ValueError_('decimal在声明scale下需要舍入，拒绝有损转换')
    except decimal.InvalidOperation:
        raise ValueError_('decimal无法按scale定标')
    unscaled = int(q.scaleb(scale))
    if len(str(abs(unscaled))) > precision:
        raise ValueError_('decimal超出precision位数')
    return unscaled, scale


def parse_timestamp(x):
    """UTC微秒整数；或带时区datetime（转UTC微秒）。朴素datetime拒绝。"""
    if type(x) is bool:
        raise ValueError_('timestamp不能是布尔')
    if type(x) is int:
        if not INT64_MIN <= x <= INT64_MAX:
            raise ValueError_('timestamp微秒超出int64范围')
        return x
    if isinstance(x, datetime.datetime):
        if x.tzinfo is None or x.utcoffset() is None:
            raise ValueError_('timestamp必须显式带时区（朴素时间拒绝）')
        epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        delta = x.astimezone(datetime.timezone.utc) - epoch
        micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        if not INT64_MIN <= micros <= INT64_MAX:
            raise ValueError_('timestamp微秒超出int64范围')
        return micros
    raise ValueError_('timestamp需要UTC微秒整数或带时区datetime')


def parse_string(x):
    if not isinstance(x, str):
        raise ValueError_('string需要JSON字符串')
    return x


def parse_bytes(x):
    if not isinstance(x, str):
        raise ValueError_('bytes需要标准base64字符串')
    try:
        return base64.b64decode(x, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError_('base64解码失败（含非法转义）')


def parse_boolean(x):
    if type(x) is not bool:
        raise ValueError_('boolean需要true/false')
    return x


def parse_value(column, x):
    if x is None:
        return None
    t = column.type
    if t == 'int64':
        return parse_int64(x)
    if t == 'decimal':
        return parse_decimal(x, column.precision, column.scale)
    if t == 'timestamp':
        return parse_timestamp(x)
    if t == 'string':
        return parse_string(x)
    if t == 'bytes':
        return parse_bytes(x)
    return parse_boolean(x)


def parse_tuple(schema, raw):
    if not isinstance(raw, list) or len(raw) != len(schema):
        raise ValueError_('键元组列数与schema不符（缺字段非法）')
    return tuple(parse_value(c, v) for c, v in zip(schema.columns, raw))


def format_value(column, v):
    """把解码出的内部值还原为清单式可读表示（演示输出用）。"""
    if v is None:
        return None
    t = column.type
    if t == 'int64':
        return v
    if t == 'decimal':
        unscaled, scale = v
        sign = '-' if unscaled < 0 else ''
        s = str(abs(unscaled)).rjust(scale + 1, '0')
        if scale == 0:
            return sign + s
        return sign + s[:-scale] + '.' + s[-scale:]
    if t == 'timestamp':
        return v
    if t == 'string':
        return v
    if t == 'bytes':
        return base64.b64encode(v).decode('ascii')
    return bool(v)
