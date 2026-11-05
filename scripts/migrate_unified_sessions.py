#!/usr/bin/env python3
"""Phase 2f unified-session migration for a copied or quiesced ``state.db`` (dry-run by default).

  python scripts/migrate_unified_sessions.py --db /path/to/state-copy.db               # plan + receipt
  python scripts/migrate_unified_sessions.py --db /path/to/state-copy.db --apply       # write, one transaction
  python scripts/migrate_unified_sessions.py --db ... --scope all --changes-out changes.jsonl

See ``hermes_state_unified_migration`` for what is migrated. One-shot: deleted after the dev3 cutover.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state_unified_migration import main

if __name__ == "__main__":
    raise SystemExit(main())
