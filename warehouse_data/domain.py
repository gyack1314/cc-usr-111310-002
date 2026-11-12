"""核对原始键元组形状，不编码或执行范围查询。"""
from .io import integer,unique
def summarize(v,root):
    unique(v['schema'],'id');unique(v['records'],'row_id');integer(v['rows_per_block'],1)
    if any(len(r['key'])!=len(v['schema']) for r in v['records']):raise ValueError('键列数不符')
    return {'key_columns':len(v['schema']),'records':len(v['records']),'rows_per_block':v['rows_per_block']}
