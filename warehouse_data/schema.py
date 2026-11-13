"""冻结键schema：列序、类型、升降序与null位置独立固定。"""
import hashlib
import json

ENCODING_VERSION = 'typedkey-v1'
TYPES = frozenset(('int64', 'decimal', 'timestamp', 'string', 'bytes', 'boolean'))


class SchemaError(ValueError):
    """schema定义非法。"""


class Column:
    __slots__ = ('name', 'type', 'direction', 'nulls', 'precision', 'scale')

    def __init__(self, spec):
        if not isinstance(spec, dict):
            raise SchemaError('列定义必须是对象')
        name = spec.get('id')
        if not isinstance(name, str) or not name:
            raise SchemaError('列id缺失或非字符串')
        ctype = spec.get('type')
        if ctype not in TYPES:
            raise SchemaError(f'未知列类型: {ctype!r}')
        direction = spec.get('direction')
        if direction not in ('asc', 'desc'):
            raise SchemaError('direction必须是asc或desc')
        nulls = spec.get('nulls')
        if nulls not in ('first', 'last'):
            raise SchemaError('nulls必须是first或last')
        precision = scale = None
        if ctype == 'decimal':
            precision = spec.get('precision')
            scale = spec.get('scale')
            if type(precision) is not int or not 1 <= precision <= 18:
                raise SchemaError('decimal precision必须是1..18整数')
            if type(scale) is not int or not 0 <= scale <= precision:
                raise SchemaError('decimal scale必须是0..precision整数')
        self.name = name
        self.type = ctype
        self.direction = direction
        self.nulls = nulls
        self.precision = precision
        self.scale = scale

    def digest_dict(self):
        d = {'id': self.name, 'type': self.type,
             'direction': self.direction, 'nulls': self.nulls}
        if self.type == 'decimal':
            d['precision'] = self.precision
            d['scale'] = self.scale
        return d


class Schema:
    """不可变schema；摘要与摘要哈希随索引持久化。"""

    def __init__(self, specs):
        if not isinstance(specs, list) or not specs:
            raise SchemaError('schema必须是非空列数组')
        self.columns = tuple(Column(s) for s in specs)
        if len({c.name for c in self.columns}) != len(self.columns):
            raise SchemaError('列id重复')
        self.summary = json.dumps(
            [c.digest_dict() for c in self.columns],
            ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.digest = hashlib.sha256(self.summary.encode('utf-8')).digest()
        self.digest_hex = self.digest.hex()

    def __len__(self):
        return len(self.columns)

    def __iter__(self):
        return iter(self.columns)

    def __getitem__(self, i):
        return self.columns[i]
