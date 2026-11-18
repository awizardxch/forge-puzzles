#!/usr/bin/env python3
"""_test_pool_admin.py's rows on V19 (protocol 20) pools: collect and the DAO-fee lower through
contracts/forge_pool_admin.py, which now spends protocols 17, 18 and 20, each pool with its own
driver (forge_pool_admin.LANES). The same suite, its pool builder and snapshot lane swapped for
V19's, so every rule the site and the fee keeper rely on is pinned for the new revision too.
Exit 0 all pass, 1 otherwise, 2 when the V19 puzzles are not built.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import _test_pool_admin as suite  # noqa: E402
import _v19_testkit as kit  # noqa: E402
import forge_pool_admin as admin  # noqa: E402
import forge_v19_driver as drv  # noqa: E402
import forge_v19_offer as off  # noqa: E402


def main() -> int:
    if not drv.v19_available():
        print("  [skip] V19 puzzles not built")
        return 2
    print("forge_pool_admin serves protocol 20:")
    suite.check("  20 is a pool-admin lane, spent by the V19 driver", admin.LANES.get(20) == (drv, off))
    suite.check("  17 and 18 keep their own lanes", 17 in admin.LANES and 18 in admin.LANES
                and admin.LANES[18][0].PROTOCOL_VERSION == 18 and admin.LANES[17][0].PROTOCOL_VERSION == 17)
    suite.kit, suite.off = kit, off          # the suite's pools and snapshots, built as V19
    code = suite.main()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
