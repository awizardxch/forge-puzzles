#!/usr/bin/env python3
"""Shared harness for the Forge V13 suites: the driver plus the consensus validator.

Every construction comes from forge_v13_driver -- the same module the testnet
deploy script uses -- so a suite proves the production driver, not a copy of
it. What this file adds is only what a test needs and production never does:
fabricated coins with fabricated parents, and `validate`, which runs a bundle
through chia_rs.get_conditions_from_spendbundle (the mempool's validator, which
enforces message pairing; it does not check coin existence, so the chain stays
the last word).
"""
from forge_v13_driver import *  # noqa: F401,F403
from forge_v13_driver import validate, Rejected, xch_settlement, cat_settlement  # noqa: F401
