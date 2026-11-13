"""查询谓词、独立逐元组解释器与半开字节区间编译。"""
from . import encoding
from .values import parse_value


class QueryError(ValueError):
    """查询形状或取值非法。"""


class Predicate:
    """前导k列精确相等（可含显式null）+ 第k+1列range或starts_with，之后无约束。"""

    __slots__ = ('equal_prefix', 'range_', 'starts_with')

    def __init__(self, equal_prefix=(), range_=None, starts_with=None):
        if range_ is not None and starts_with is not None:
            raise QueryError('range与starts_with不可同时出现')
        self.equal_prefix = tuple(equal_prefix)
        self.range_ = range_            # (lower, lower_closed, upper, upper_closed)
        self.starts_with = starts_with  # 已解析的str/bytes

    @classmethod
    def from_manifest(cls, schema, q):
        if not isinstance(q, dict):
            raise QueryError('query必须是对象')
        if 'range' in q and 'starts_with' in q:
            raise QueryError('range与starts_with不可同时出现')
        eq_raw = q.get('equal_prefix', [])
        if not isinstance(eq_raw, list) or not eq_raw:
            raise QueryError('equal_prefix必须是非空数组')
        k = len(eq_raw)
        if k > len(schema):
            raise QueryError('equal_prefix列数超过schema')
        eq = tuple(parse_value(schema[i], v) for i, v in enumerate(eq_raw))
        range_ = sw = None
        if 'range' in q or 'starts_with' in q:
            if k == len(schema):
                raise QueryError('全部列已精确相等时不能再挂range/starts_with')
            rcol = schema[k]
            if 'range' in q:
                r = q['range']
                if not isinstance(r, dict):
                    raise QueryError('range必须是对象')
                lo_raw, hi_raw = r.get('lower'), r.get('upper')
                lc = bool(r.get('lower_closed', lo_raw is not None))
                uc = bool(r.get('upper_closed', hi_raw is not None))
                lo = None if lo_raw is None else parse_value(rcol, lo_raw)
                hi = None if hi_raw is None else parse_value(rcol, hi_raw)
                if lo is None or hi is None:
                    raise QueryError('range界不接受null：null不属于普通大小比较')
                range_ = (lo, lc, hi, uc)
            else:
                s = q['starts_with']
                if rcol.type not in ('string', 'bytes'):
                    raise QueryError('starts_with只支持string/bytes列')
                if rcol.type == 'bytes':
                    if not isinstance(s, dict) or 'base64' not in s:
                        raise QueryError('bytes前缀必须写作{"base64": ...}')
                    sw = parse_value(rcol, s['base64'])
                else:
                    if not isinstance(s, str):
                        raise QueryError('string前缀必须是字符串')
                    sw = s
        return cls(eq, range_, sw)

    def range_column_index(self, schema):
        return len(self.equal_prefix)


def _compare(a, b):
    return (a > b) - (a < b)


def _matches_column(column, v, pred: Predicate):
    """对单列解释range/starts_with；自然类型序，null永不命中普通范围。"""
    if v is None:
        return False
    if pred.starts_with is not None:
        if column.type == 'string':
            return isinstance(v, str) and v.startswith(pred.starts_with)
        return isinstance(v, bytes) and v.startswith(pred.starts_with)
    lo, lc, hi, uc = pred.range_
    if _compare(v, lo) < (0 if lc else 1):
        return False
    if _compare(v, hi) > (0 if uc else -1):
        return False
    return True


def evaluate(schema, values, pred: Predicate):
    """独立逐元组谓词解释器：不读字节区间，作为区间正确性的核对基准。"""
    if len(values) != len(schema):
        return False
    k = len(pred.equal_prefix)
    for i, want in enumerate(pred.equal_prefix):
        got = values[i]
        if want is None:
            if got is not None:
                return False
        elif got is None or got != want:
            return False
    if pred.range_ is None and pred.starts_with is None:
        return True
    return _matches_column(schema[k], values[k], pred)


def _escaped_prefix(column, p) -> bytes:
    raw = p.encode('utf-8') if column.type == 'string' else p
    return encoding._escape(raw)


def _natural_empty(column, pred: Predicate):
    """在自然序空间先判定语义为空（只用于range；前缀条件本身非空）。"""
    if pred.range_ is None:
        return False
    lo, lc, hi, uc = pred.range_
    if lo < hi:
        return False
    if lo > hi:
        return True
    return not (lc and uc)  # 等值但任一端开区间 => 空


def _asc_span(column, pred: Predicate):
    if pred.starts_with is not None:
        p = _escaped_prefix(column, pred.starts_with)
        lo = bytes([encoding._TAGS[column.type]]) + p
        return lo, encoding.successor(lo)  # tag非0xFF，恒有有限后继
    lo_v, lc, hi_v, uc = pred.range_
    nlo = encoding.encode_column_natural(column, lo_v)
    nhi = encoding.encode_column_natural(column, hi_v)
    lo = nlo if lc else encoding.successor(nlo)
    hi = encoding.successor(nhi) if uc else nhi
    return lo, hi


def _desc_span(column, pred: Predicate):
    """desc空间直接从完整合法列编码推导，不对残缺前缀取反。"""
    if pred.starts_with is not None:
        # 所有命中文的desc编码共享该前缀（转义不变量保证无其他键落入）
        prefix = encoding._invert(
            bytes([encoding._TAGS[column.type]]) + _escaped_prefix(column, pred.starts_with))
        return prefix, encoding.successor(prefix)
    lo_v, lc, hi_v, uc = pred.range_
    d_lo = encoding.encode_column_natural(column, lo_v)  # 自然下界对应desc上界
    d_hi = encoding.encode_column_natural(column, hi_v)  # 自然上界对应desc下界
    upper = encoding.successor(encoding._invert(d_lo)) if lc else encoding._invert(d_lo)
    lower = encoding._invert(d_hi) if uc else encoding.successor(encoding._invert(d_hi))
    return lower, upper


def compile_intervals(schema, pred: Predicate):
    """编译为有限个半开字节区间[(lo,hi)]，None端表示无界；语义为空则返回[]。"""
    k = pred.range_column_index(schema)
    has_constraint = pred.range_ is not None or pred.starts_with is not None
    if has_constraint and _natural_empty(schema[k], pred):
        return []
    prefix = b''.join(
        encoding.encode_column(schema[i], pred.equal_prefix[i]) for i in range(k))
    if k == len(schema) or not has_constraint:
        # 全键相等，或前导相等后剩余列无约束：前缀到其后继定界整个子空间
        return [(prefix, encoding.successor(prefix))]
    rcol = schema[k]
    lo, hi = _asc_span(rcol, pred) if rcol.direction == 'asc' else _desc_span(rcol, pred)
    lo = prefix + lo if lo is not None else None
    hi = prefix + hi if hi is not None else None
    if lo is not None and hi is not None and lo >= hi:
        return []
    return [(lo, hi)]
