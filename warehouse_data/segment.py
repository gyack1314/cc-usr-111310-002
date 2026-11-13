"""外存有序索引段：外排合并、块级稀疏目录、范围seek与完整性校验。

段文件布局：
  文件头(magic/schema摘要/编码版本/快照/区段偏移长度/块数/CRC32)
  数据区(若干块；每块=帧化完整条目+CRC32)
  目录(每块首键/条目数/数据区偏移的帧化序列+CRC32)
  文件尾(magic + 数据区SHA256)
损坏（截断、CRC不符、magic不符、尾随未解释字节）一律抛SegmentCorruptError，
绝不返回空结果冒充。
"""
import hashlib
import heapq
import os
import struct
import tempfile
import zlib
from collections import OrderedDict

from . import encoding

MAGIC = b'WDTKSEG01\x00\x00\x00\x00\x00\x00\x00'
MIN_WORKING_BUDGET = 64 * 1024
HARD_ROW_BYTES = 16 * 1024 * 1024
_DLEN = struct.Struct('>I')
_POS = struct.Struct('>Q')
_CRC = struct.Struct('>I')


class SegmentError(Exception):
    """段构建/资源问题。"""


class SegmentCorruptError(Exception):
    """段文件损坏。"""


class ResourceLimitError(Exception):
    """内存或打开文件数超限。"""


def _frame(blob: bytes) -> bytes:
    if len(blob) > 0xFFFFFFFF:
        raise SegmentError('帧过长')
    return _DLEN.pack(len(blob)) + blob


def _put_frame(b: bytearray, blob: bytes):
    b += _frame(blob)


def _crc32(blob: bytes) -> int:
    return zlib.crc32(blob) & 0xFFFFFFFF


def _read_exact(f, n, where):
    data = f.read(n)
    if len(data) != n:
        raise SegmentCorruptError(f'截断：{where}期望{n}字节，实得{len(data)}')
    return data


def _read_frame(f, where='帧'):
    head = _read_exact(f, 4, where)
    (n,) = _DLEN.unpack(head)
    return _read_exact(f, n, where)


def _entry_blob(entry_key: bytes, row_id: str, position: int) -> bytes:
    if not (0 <= position <= 0xFFFFFFFFFFFFFFFF):
        raise SegmentError('source_position越界')
    rid = encoding.encode_row_id(row_id)
    return _frame(entry_key) + _frame(rid) + _POS.pack(position)


def _parse_entry(blob: bytes):
    pos = 0
    if len(blob) < 4:
        raise SegmentCorruptError('截断：条目键长度')
    (kl,) = _DLEN.unpack(blob[pos:pos + 4]); pos += 4
    if pos + kl > len(blob):
        raise SegmentCorruptError('截断：条目键')
    entry_key = blob[pos:pos + kl]; pos += kl
    if pos + 4 > len(blob):
        raise SegmentCorruptError('截断：row_id长度')
    (rl,) = _DLEN.unpack(blob[pos:pos + 4]); pos += 4
    if pos + rl > len(blob):
        raise SegmentCorruptError('截断：row_id')
    rid_blob = blob[pos:pos + rl]; pos += rl
    if pos + 8 > len(blob):
        raise SegmentCorruptError('截断：position')
    (position,) = _POS.unpack(blob[pos:pos + 8]); pos += 8
    if pos != len(blob):
        raise SegmentCorruptError('尾随未解释字节：条目帧后有残余')
    if not rid_blob or rid_blob[-1] != 0x00:
        raise SegmentCorruptError('row_id帧终结符缺失')
    return entry_key, rid_blob, position


class BlockCache:
    """解码块字节缓存；超预算LRU淘汰，保证至少缓存当前块。"""

    def __init__(self, byte_budget):
        self.budget = max(byte_budget, MIN_WORKING_BUDGET)
        self.used = 0
        self.od = OrderedDict()

    def get(self, index, loader):
        if index in self.od:
            self.od.move_to_end(index)
            return self.od[index]
        blob = loader()
        if len(blob) > HARD_ROW_BYTES:
            raise SegmentCorruptError('块超过硬上限')
        self.used += len(blob)
        self.od[index] = blob
        while self.used > self.budget and len(self.od) > 1:
            _, old = self.od.popitem(last=False)
            self.used -= len(old)
        return blob


class FilePool:
    """打开文件数硬上限的LRU句柄池。"""

    def __init__(self, max_open):
        if max_open < 2:
            raise ResourceLimitError('max_open_files至少为2')
        self.max_open = max_open
        self.od = OrderedDict()

    def open(self, path):
        path = os.fspath(path)
        if path in self.od:
            self.od.move_to_end(path)
            return self.od[path]
        if len(self.od) >= self.max_open:
            _, old_f = self.od.popitem(last=False)
            old_f.close()
        f = open(path, 'rb')
        self.od[path] = f
        return f

    def close(self):
        for f in self.od.values():
            f.close()
        self.od.clear()

    def __len__(self):
        return len(self.od)


class SegmentBuilder:
    """外存归并排序构建单段；内存预算外的缓冲落临时run文件。"""

    def __init__(self, path, schema, snapshot, rows_per_block,
                 memory_bytes, max_open_files):
        if rows_per_block < 1:
            raise SegmentError('rows_per_block必须>=1')
        self.path = path
        self.schema = schema
        self.snapshot = snapshot
        self.rows_per_block = rows_per_block
        self.byte_budget = max(memory_bytes, MIN_WORKING_BUDGET)
        self.pool = FilePool(max_open_files)
        self.buffer = []
        self.buffer_bytes = 0
        self.runs = []
        self.total_rows = 0
        self._tmpdir = tempfile.mkdtemp(prefix='wdtk-runs-')

    def add(self, values, row_id, position):
        entry_key = encoding.encode_entry_key(self.schema, values, row_id)
        if len(entry_key) > HARD_ROW_BYTES:
            raise ResourceLimitError('单行超过硬上限')
        self.buffer.append((entry_key, position))
        self.buffer_bytes += len(entry_key)
        self.total_rows += 1
        if self.buffer_bytes >= self.byte_budget:
            self._flush_run()

    def _flush_run(self):
        if not self.buffer:
            return
        self.buffer.sort(key=lambda r: r[0])
        run_path = os.path.join(self._tmpdir, f'run-{len(self.runs)}.wdtk')
        with open(run_path, 'wb') as f:
            for key, pos in self.buffer:
                f.write(_frame(key) + _POS.pack(pos))
        self.runs.append(run_path)
        self.buffer.clear()
        self.buffer_bytes = 0

    def _iter_merged(self):
        if not self.runs:
            yield from sorted(self.buffer, key=lambda r: r[0])
            return
        if self.buffer:
            self._flush_run()

        def iter_run(path):
            f = self.pool.open(path)
            f.seek(0)
            size = os.path.getsize(path)
            while f.tell() < size:
                key = _read_frame(f, 'run键帧')
                (pos,) = _POS.unpack(_read_exact(f, 8, 'run position'))
                yield key, pos

        yield from heapq.merge(*(iter_run(p) for p in self.runs),
                               key=lambda r: r[0])

    def finish(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        directory = []
        data_hasher = hashlib.sha256()
        with open(self.path, 'wb') as out:
            header_len = self._header_len()
            out.write(b'\x00' * header_len)
            data_start = out.tell()
            block = bytearray()
            block_rows = 0
            block_start = data_start
            block_first = None

            def flush_block():
                nonlocal block, block_rows, block_start, block_first
                if not block_rows:
                    return
                block += _CRC.pack(_crc32(bytes(block)))
                data_hasher.update(block)
                out.write(block)
                directory.append((block_first, block_rows, block_start))
                block = bytearray()
                block_rows = 0
                block_start = out.tell()  # 新块起点：CRC写入之后的位置
                block_first = None
            for key, pos in self._iter_merged():
                if block_rows >= self.rows_per_block:
                    flush_block()
                if block_first is None:
                    block_first = key
                row_id = encoding.split_entry_key(self.schema, key)[1]
                block += _frame(_entry_blob(key, row_id, pos))
                block_rows += 1
            flush_block()
            data_len = out.tell() - data_start
            data_digest = data_hasher.digest()

            dir_blob = bytearray()
            for first_key, rows, offset in directory:
                _put_frame(dir_blob, first_key)
                dir_blob += _DLEN.pack(rows) + _POS.pack(offset)
            dir_offset = out.tell()
            out.write(_frame(bytes(dir_blob)) + _CRC.pack(_crc32(bytes(dir_blob))))
            dir_len = out.tell() - dir_offset
            out.write(MAGIC + data_digest)
            self._write_header(out, header_len, data_start, data_len,
                               dir_offset, dir_len, len(directory))
        self._cleanup_runs()
        self.pool.close()
        return len(directory)

    def _header_parts(self):
        version_frame = _frame(encoding.CODEC_VERSION.encode('utf-8'))
        snapshot_frame = _frame(self.snapshot.encode('utf-8'))
        return version_frame, snapshot_frame

    def _header_len(self):
        version_frame, snapshot_frame = self._header_parts()
        return (len(MAGIC) + 32 + len(version_frame) + len(snapshot_frame)
                + 5 * 8 + 4)

    def _write_header(self, out, header_len, data_start, data_len,
                      dir_offset, dir_len, block_count):
        version_frame, snapshot_frame = self._header_parts()
        body = (MAGIC + self.schema.digest + version_frame + snapshot_frame
                + _POS.pack(data_start) + _POS.pack(data_len)
                + _POS.pack(dir_offset) + _POS.pack(dir_len)
                + _POS.pack(block_count))
        header = body + _CRC.pack(_crc32(body))
        if len(header) != header_len:
            raise SegmentError('头部长度计算错误')
        out.seek(0)
        out.write(header)
        out.seek(0, os.SEEK_END)

    def _cleanup_runs(self):
        for p in self.runs:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(self._tmpdir)
        except OSError:
            pass


class SegmentReader:
    """打开并校验段；目录二分定位相交块，重复键跨块完整返回。"""

    def __init__(self, path, schema, pool: FilePool, cache: BlockCache):
        self.path = os.fspath(path)
        self.schema = schema
        self.pool = pool
        self.cache = cache
        self._open_and_verify()

    def _f(self):
        return self.pool.open(self.path)

    def _open_and_verify(self):
        f = self._f()
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        f.seek(0)
        if file_size < len(MAGIC):
            raise SegmentCorruptError('段文件短于magic')
        head_magic = _read_exact(f, len(MAGIC), '文件头magic')
        if head_magic != MAGIC:
            raise SegmentCorruptError('文件头magic不符')
        schema_digest = _read_exact(f, 32, 'schema摘要')
        if schema_digest != self.schema.digest:
            raise SegmentCorruptError('段schema摘要与当前schema不符')
        version = _read_frame(f, '编码版本').decode('utf-8')
        if version != encoding.CODEC_VERSION:
            raise SegmentCorruptError(
                f'段编码版本{version!r}与当前{encoding.CODEC_VERSION!r}不兼容')
        self.snapshot = _read_frame(f, '快照').decode('utf-8')
        (self.data_start, self.data_len, self.dir_offset,
         self.dir_len, self.block_count) = struct.unpack(
            '>5Q', _read_exact(f, 40, '头部定位字段'))
        header_end = f.tell()
        (header_crc,) = _CRC.unpack(_read_exact(f, 4, '头部CRC'))
        f.seek(0)
        header_body = _read_exact(f, header_end, '头部体')
        if _crc32(header_body) != header_crc:
            raise SegmentCorruptError('文件头CRC不符')
        if not (self.data_start == header_end + 4
                and self.dir_offset == self.data_start + self.data_len
                and self.dir_offset + self.dir_len + len(MAGIC) + 32 == file_size):
            raise SegmentCorruptError('区段定位不一致或存在尾随未解释字节')
        f.seek(file_size - len(MAGIC) - 32)
        tail_magic = _read_exact(f, len(MAGIC), '文件尾magic')
        if tail_magic != MAGIC:
            raise SegmentCorruptError('文件尾magic不符')
        self.data_digest = _read_exact(f, 32, '数据区摘要')
        # 打开即全量核对数据区SHA-256：任何数据字节翻转都在打开时拒绝，而非惰性放过
        f.seek(self.data_start)
        hasher = hashlib.sha256()
        remaining = self.data_len
        while remaining:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                raise SegmentCorruptError('截断：数据区不足')
            hasher.update(chunk)
            remaining -= len(chunk)
        if hasher.digest() != self.data_digest:
            raise SegmentCorruptError('数据区SHA256不符，段已损坏')
        f.seek(self.dir_offset)
        dir_blob = _read_frame(f, '目录')
        (dir_crc,) = _CRC.unpack(_read_exact(f, 4, '目录CRC'))
        if _crc32(dir_blob) != dir_crc:
            raise SegmentCorruptError('目录CRC不符')
        self.directory = self._parse_directory(dir_blob)
        if len(self.directory) != self.block_count:
            raise SegmentCorruptError('目录块数与头部不符')

    def _parse_directory(self, blob):
        entries = []
        pos = 0
        for _ in range(self.block_count):
            if pos + 4 > len(blob):
                raise SegmentCorruptError('截断：目录键长度')
            (kl,) = _DLEN.unpack(blob[pos:pos + 4]); pos += 4
            if pos + kl > len(blob):
                raise SegmentCorruptError('截断：目录键')
            first_key = blob[pos:pos + kl]; pos += kl
            if pos + 12 > len(blob):
                raise SegmentCorruptError('截断：目录定位')
            rows, offset = struct.unpack('>IQ', blob[pos:pos + 12]); pos += 12
            entries.append((first_key, rows, offset))
        if pos != len(blob):
            raise SegmentCorruptError('尾随未解释字节：目录残余')
        return entries

    def _load_block(self, index):
        first_key, rows, offset = self.directory[index]
        f = self._f()
        f.seek(offset)
        block = bytearray()
        for _ in range(rows):
            head = _read_exact(f, 4, '条目帧长度')
            (n,) = _DLEN.unpack(head)
            block += head + _read_exact(f, n, '块内条目帧')
        (stored_crc,) = _CRC.unpack(_read_exact(f, 4, '块CRC'))
        if _crc32(bytes(block)) != stored_crc:
            raise SegmentCorruptError(f'块{index} CRC不符')
        entries = []
        while block:
            (n,) = _DLEN.unpack(block[:4])
            end = 4 + n
            if end > len(block):
                raise SegmentCorruptError('截断：块内条目')
            entries.append(_parse_entry(bytes(block[4:end])))
            del block[:end]
        if entries and entries[0][0] != first_key:
            raise SegmentCorruptError(f'块{index}首键与目录不符')
        return entries

    def _block_at_or_before(self, key):
        lo, hi = 0, len(self.directory)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.directory[mid][0] <= key:
                lo = mid + 1
            else:
                hi = mid
        return max(0, lo - 1)

    def scan(self, intervals):
        """对有限个半开区间seek并顺序扫描，产出(解码元组,row_id,position,块号)。"""
        for lo, hi in intervals:
            start_block = 0 if lo is None else self._block_at_or_before(lo)
            for bi in range(start_block, len(self.directory)):
                first_key = self.directory[bi][0]
                if hi is not None and first_key >= hi:
                    break  # 稀疏目录：本块及之后整块落在上界外
                entries = self.cache.get(bi, lambda i=bi: self._load_block(i))
                for entry_key, _rid_blob, position in entries:
                    if lo is not None and entry_key < lo:
                        continue
                    if hi is not None and entry_key >= hi:
                        break
                    values, row_id = encoding.split_entry_key(
                        self.schema, entry_key)
                    yield values, row_id, position, bi
