# 复合仓储键的保序编码与范围索引

此项目保存生产与供应链协同的湖仓原始资料。`warehouse_data.io`读取JSON清单及核对文件身份，`warehouse_data.domain`检查基本形状并输出数量。在此之上，本项目实现有类型复合键的保序字节编码、半开区间编译与外存有序索引段：

- `schema.py`：冻结列序、类型、升降序与null位置（方向与null位置相互独立）。
- `values.py`：清单原文到有类型值；int64边界、decimal按scale精确定标（需舍入即拒绝）、UTC微秒时间戳、严格base64、boolean。
- `encoding.py`：`typedkey-v1`保序编码，无歧义、可逆、自定界；无符号字节序等价于schema业务序。
- `predicate.py`：前导相等+单列range/starts_with查询，编译为有限个半开字节区间，并附独立逐元组谓词解释器交叉核对。
- `segment.py`：外存归并排序索引段、块级稀疏目录seek、CRC32/SHA256校验、内存与打开文件数上限。
- `index.py`：绑定数据快照、schema摘要与编码版本；源或schema变更使旧索引失效。
- `demo.py`：负边界、decimal等值异写、零字节、升降混排、重复键跨块与大样本块相交演示。

编码的正式版本承诺见[编码规范](docs/encoding-typedkey-v1.md)，资料字段语义见[资料约定](docs/data-contract.md)。设备、批次、组织、事件和成本均为合成，不连接真实工厂或生产仓库。

Python3.11以上，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q warehouse_data tests
python -m warehouse_data.cli inspect data/normal.json
python -m warehouse_data.cli build data/normal.json build/idx
python -m warehouse_data.cli query data/normal.json build/idx
python -m warehouse_data.cli demo
```

`inspect`只读原始资料，不生成仓库新版本或计算领域结果。`build`把清单编码为外存索引段（含schema摘要、编码版本、快照与源摘要）；`query`把清单内query编译为半开字节区间，经稀疏目录seek扫描后再用独立谓词解释器复核，输出原元组、编码、实际读取块与原行引用。`demo`内置端到端演示。`invalid-config.json`有基本形状错误；其余清单有完整资料身份，但可能包含需要业务操作判断的冲突、缺口或不满足条件的边界。null与缺字段分别保留，有理数和decimal用字符串存储，不能先经float丢精度。

Dockerfile固定Linux镜像，`docker build -t warehouse-material .`构建；`docker run --rm warehouse-material`显示摘要；`docker run --rm warehouse-material python -m unittest discover -s tests -v`运行已有检查。所有资料本地可读，不需要账号、外部数据库或在线数据源。
