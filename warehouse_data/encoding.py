"""typedkey-v1 保序编码。

每列自然序编码 = 类型tag + 转义payload + 0x00自定界符；显式null为单字节
null-tag。0x00/0x01分别转义为01 00/01 01，保证终结符与转义对无歧义。
desc列对整段字节取反（含tag与终结符），null位置由独立null-tag决定，不随
方向反转。元组按冻结列序拼接；任意两个合法元组的无符号字节比较与schema
业务比较等价，且共享前导列的元组具有真实字节前缀。
"""
from .values import INT64_MIN, INT64_MAX

CODEC_VERSION = 'typedkey-v1'


class CodecError(ValueError):
    """编码截断、非法转义、尾随未解释字节或越界。"""


_ESC = 0x01
_TERM = 0x00

# 自然序（asc/nulls-first视角）tag，均为偶值，非null tag位于null tag之间。
_NULL_FIRST_TAG = 0x02
_NULL_LAST_TAG = 0x20
# desc空间的null tag独立指定，绝不随非null值取反而反转：
# 非null desc首字节集合为{F1,F3,F5,F7,F9,FB}，0x00恒在其前、0xFC恒在其后。
_DESC_NULL_FIRST_TAG = 0x00
_DESC_NULL_LAST_TAG = 0xFC
_TAGS = {
    'boolean': 0x04,
    'int64': 0x06,
    'timestamp': 0x08,
    'decimal': 0x0A,
    'string': 0x0C,
    'bytes': 0x0E,
}
_DESC_VALUE_FIRST_BYTES = frozenset(0xFF - t for t in _TAGS.values())


def _escape(raw: bytes) -> bytes:
    return raw.replace(b'\x01', b'\x01\x01').replace(b'\x00', b'\x01\x00')


def _unescape(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b != _ESC:
            out.append(b)
            i += 1
        else:
            if i + 1 >= len(raw):
                raise CodecError('截断：转义前缀后无字节')
            nxt = raw[i + 1]
            if nxt not in (0x00, 0x01):
                raise CodecError('非法转义对')
            out.append(nxt)
            i += 2
    return bytes(out)


def _invert(raw: bytes) -> bytes:
    return bytes(b ^ 0xFF for b in raw)


def _signed_varint(n: int) -> bytes:
    """有符号整数 -> 保序变长字节（SQLite4式标记字节），随后整体再过转义。"""
    if n < 0:
        mag = (-n).to_bytes((-n).bit_length() // 8 + 1, 'big', signed=False)
        marker = 0x0B - len(mag)  # L=1..9 -> 0x0A..0x02，绝对值越大越靠前
        return bytes([marker]) + _invert(mag)
    raw = n.to_bytes(max(1, (n.bit_length() + 7) // 8), 'big', signed=False)
    marker = 0x16 + len(raw)     # L=1..9 -> 0x17..0x1F，位数越多越靠后
    return bytes([marker]) + raw


def _read_signed_varint(buf: bytes, pos: int):
    marker = buf[pos]
    pos += 1
    if marker <= 0x0A:
        length = 0x0B - marker
        if pos + length > len(buf):
            raise CodecError('截断：负整数载荷不足')
        n = -int.from_bytes(_invert(buf[pos:pos + length]), 'big')
        pos += length
        return n, pos
    if marker >= 0x17:
        length = marker - 0x16
        if pos + length > len(buf):
            raise CodecError('截断：正整数载荷不足')
        n = int.from_bytes(buf[pos:pos + length], 'big')
        pos += length
        return n, pos
    raise CodecError('保留标记字节出现，编码被损坏或版本不符')


def _encode_natural_payload(column, v) -> bytes:
    t = column.type
    if t == 'boolean':
        return bytes([0x02 if v is False else 0x03])
    if t == 'int64':
        if not INT64_MIN <= v <= INT64_MAX:
            raise CodecError('int64越界')
        return _signed_varint(v)
    if t == 'timestamp':
        if not INT64_MIN <= v <= INT64_MAX:
            raise CodecError('timestamp微秒越界')
        return _signed_varint(v)
    if t == 'decimal':
        unscaled, scale = v
        if scale != column.scale:
            raise CodecError('decimal scale与schema不符')
        if len(str(abs(unscaled))) > column.precision:
            raise CodecError('decimal超precision')
        return _signed_varint(unscaled)
    if t == 'string':
        return v.encode('utf-8')  # 原UTF8字节序，不做Unicode归一
    if t == 'bytes':
        return v  # 允许任意0字节
    raise CodecError(f'未知类型 {t}')


def _decode_natural_payload(column, buf: bytes, pos: int):
    t = column.type
    if t == 'boolean':
        b = buf[pos]
        if b not in (0x02, 0x03):
            raise CodecError('boolean载荷非法')
        return (b == 0x03), pos + 1
    if t in ('int64', 'timestamp', 'decimal'):
        n, pos = _read_signed_varint(buf, pos)
        if t == 'int64' and not INT64_MIN <= n <= INT64_MAX:
            raise CodecError('int64越界')
        if t == 'decimal':
            if len(str(abs(n))) > column.precision:
                raise CodecError('decimal超precision')
            return (n, column.scale), pos
        return n, pos
    # string/bytes的payload即终结符前的全部转义字节，由调用方切给unescape
    raise CodecError('变长载荷应由列解码器处理')


def _null_tag(column) -> int:
    return _NULL_FIRST_TAG if column.nulls == 'first' else _NULL_LAST_TAG


def encode_column_natural(column, v) -> bytes:
    """单列自然序编码（含终结符；null为单字节）。"""
    if v is None:
        return bytes([_null_tag(column)])
    tag = _TAGS[column.type]
    return bytes([tag]) + _escape(_encode_natural_payload(column, v)) + bytes([_TERM])


def encode_column(column, v) -> bytes:
    """按列方向落盘的单列编码；desc的null位置独立固定，不被取反反转。"""
    if v is None:
        if column.direction == 'desc':
            return bytes([_DESC_NULL_FIRST_TAG if column.nulls == 'first'
                          else _DESC_NULL_LAST_TAG])
        return bytes([_null_tag(column)])
    natural = encode_column_natural(column, v)
    return _invert(natural) if column.direction == 'desc' else natural


def decode_column(column, buf: bytes, pos: int):
    """从pos解码一列，返回(值或None, 新pos)。严格拒绝截断与非法转义。"""
    if pos >= len(buf):
        raise CodecError('截断：列首字节缺失')
    first = buf[pos]
    desc = column.direction == 'desc'
    if desc:
        if first == _DESC_NULL_FIRST_TAG and column.nulls == 'first':
            return None, pos + 1
        if first == _DESC_NULL_LAST_TAG and column.nulls == 'last':
            return None, pos + 1
        if first not in _DESC_VALUE_FIRST_BYTES:
            raise CodecError('列tag与schema不符或编码损坏')
        natural_first = first ^ 0xFF
        if natural_first != _TAGS[column.type]:
            raise CodecError('列类型tag与schema不符')
    else:
        if first == _null_tag(column):
            return None, pos + 1
        if first != _TAGS[column.type]:
            raise CodecError('列类型tag与schema不符或编码损坏')
    # 在对应空间找终结符（自然空间0x00；取反空间0xFF，且不能误判转义对内部）
    esc = _ESC ^ (0xFF if desc else 0)
    term = _TERM ^ (0xFF if desc else 0)
    i = pos + 1
    while True:
        b = buf[i] if i < len(buf) else None
        if b is None:
            raise CodecError('截断：缺列终结符')
        if b == esc:
            if i + 1 >= len(buf):
                raise CodecError('截断：转义对不完整')
            i += 2
            continue
        if b == term:
            end = i
            break
        i += 1
    natural = _invert(buf[pos:end + 1]) if desc else buf[pos:end + 1]
    tag = natural[0]
    if tag != _TAGS[column.type]:
        raise CodecError('列类型tag与schema不符')
    payload = _unescape(natural[1:-1])
    pos = end + 1
    if column.type in ('string', 'bytes'):
        v = payload.decode('utf-8') if column.type == 'string' else payload
        return v, pos
    inner_pos = 0
    val, inner_pos = _decode_natural_payload(column, payload, inner_pos)
    if inner_pos != len(payload):
        raise CodecError('尾随未解释字节：定长载荷后有残余')
    return val, pos


def encode_tuple(schema, values) -> bytes:
    if not isinstance(values, (tuple, list)) or len(values) != len(schema):
        raise CodecError('输入必须完整：缺字段非法')
    return b''.join(encode_column(c, v) for c, v in zip(schema.columns, values))


def decode_tuple(schema, data: bytes, full: bool = True):
    out = []
    pos = 0
    for column in schema.columns:
        v, pos = decode_column(column, data, pos)
        out.append(v)
    if full and pos != len(data):
        raise CodecError('尾随未解释字节：元组后仍有数据')
    return tuple(out), pos


def encode_row_id(row_id: str) -> bytes:
    if not isinstance(row_id, str) or not row_id:
        raise CodecError('row_id必须是非空字符串')
    return _escape(row_id.encode('utf-8')) + bytes([_TERM])


def encode_entry_key(schema, values, row_id: str) -> bytes:
    """业务键 + 稳定row_id后缀；row_id不改变业务键前缀的任何区间边界。"""
    return encode_tuple(schema, values) + encode_row_id(row_id)


def split_entry_key(schema, data: bytes):
    values, pos = decode_tuple(schema, data, full=False)
    tail = data[pos:]
    if not tail or tail[-1] != _TERM:
        raise CodecError('row_id后缀截断或终结符缺失')
    try:
        row_id = _unescape(tail[:-1]).decode('utf-8')
    except UnicodeDecodeError:
        raise CodecError('row_id不是合法UTF8')
    if not row_id:
        raise CodecError('row_id为空')
    return values, row_id


def successor(b: bytes):
    """最短有限后继：低位首个非0xFF字节加一；全0xFF无有限后继。"""
    for i in range(len(b) - 1, -1, -1):
        if b[i] != 0xFF:
            return b[:i] + bytes([b[i] + 1])
    return None
