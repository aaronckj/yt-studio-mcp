"""`yt-studio-mcp draw`: the giveaway draw from the command line (Cipher Studio runs it on the box that holds the snapshots).

Runs tools.giveaway.draw_winners_record, the SAME function the MCP tool draw_winners calls: same v2 sha256 ranking, same
snapshot checks, same hash-chained draw log, same audit record. The seed is read from stdin (one line) so it never sits in a
process listing before it is public; it must already have been announced. Prints the result as one JSON object.
Exit 0 = drawn (or the identical draw re-run), 2 = refused (the JSON says why), 1 = usage error.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="yt-studio-mcp draw")
    p.add_argument("--snapshot", required=True, help="snapshot file name or path inside the giveaways folder")
    p.add_argument("--n", type=int, required=True, help="number of winners (distinct channels)")
    p.add_argument("--redraw-reason", default="", help="required when this snapshot was already drawn with another seed")
    p.add_argument("--allow-incomplete", action="store_true")
    a = p.parse_args(argv)
    seed = sys.stdin.readline().strip()
    from .tools.giveaway import draw_winners_record

    out = draw_winners_record(a.snapshot, n=a.n, seed=seed, redraw_reason=a.redraw_reason, allow_incomplete=a.allow_incomplete)
    print(json.dumps(out))
    return 2 if "error" in out else 0
