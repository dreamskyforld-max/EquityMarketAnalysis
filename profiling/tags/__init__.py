#!/usr/bin/env python3
"""标签域实现。导入本包即触发全部标签注册（装饰器副作用）。

已实现：
    ① 证券属性  →  tags/identity.py     12 个标签
    ② 规模属性  →  tags/scale.py          6 个标签
    ③ 估值水平  →  tags/valuation.py      9 个标签
    ④ 盈利质量  →  tags/quality.py       10 个标签
    ⑤ 成长特征  →  tags/growth.py         8 个标签
    ⑥ 股东回报  →  tags/shareholder.py    6 个标签
    ⑦ 交易特征  →  tags/technical.py      9 个标签
    ⑨ 资金关注度 →  tags/attention.py     14 个标签
    ⑩ 趋势状态  →  tags/trend.py          1 个标签（6 阶段状态机待验证闭环，暂不注册）

新增域时：在 tags/ 下新建模块，并在本文件 import，标签字典自动同步。
"""
from . import identity     # noqa: F401  导入即注册
from . import scale        # noqa: F401
from . import valuation    # noqa: F401
from . import quality      # noqa: F401
from . import growth       # noqa: F401
from . import shareholder  # noqa: F401
from . import technical    # noqa: F401
from . import attention    # noqa: F401
from . import trend        # noqa: F401
