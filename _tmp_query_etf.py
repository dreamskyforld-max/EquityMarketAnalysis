import sys
sys.path.insert(0, '.')
import signal
from collections import Counter
from collector_runtime import get_shared_ctx
from futu import RET_OK

def handler(signum, frame):
    raise TimeoutError("timeout 60s")
signal.signal(signal.SIGALRM, handler)
signal.alarm(60)

ctx = get_shared_ctx()
for m in ('SH', 'SZ'):
    try:
        ret, d = ctx.get_stock_basicinfo(m, 'ETF')
    except Exception as e:
        print(m, 'EXC:', repr(e))
        continue
    if ret != RET_OK:
        print(m, 'FAILED:', d)
        continue
    codes = [str(c) for c in d['code']]
    print(m, 'ETF 数量:', len(codes))
    seg = Counter(c.split('.', 1)[1][:3] for c in codes)
    print('  号码段分布(前3位):', dict(seg.most_common(15)))
signal.alarm(0)
