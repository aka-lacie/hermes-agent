
import os, sys
sys.path.insert(0, '.')
from tools.registry import registry
import model_tools

for name in sorted(registry._tools.keys()):
    entry = registry._tools[name]
    if entry.check_fn:
        try:
            r = entry.check_fn()
            status = 'PASS' if r else 'FAIL'
        except Exception as e:
            status = 'ERROR: %s: %s' % (type(e).__name__, e)
    else:
        status = 'NO_CHECK'
    print('%s | toolset=%-15s | check=%s' % (name.ljust(30), entry.toolset, status))
