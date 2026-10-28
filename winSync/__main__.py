import argparse
import logging
import os
import sys
import threading

from aiohttp import web

from . import omnirig, sdrconsole
from .server import build_app, load_config


def exit_when_parent_goes():
    """Started by the winSync window with a pipe as stdin: when the window closes (or crashes),
    the pipe closes, and the server must not be left running and holding the port."""
    def watch():
        try:
            while sys.stdin.buffer.read(1024):
                pass
        except (OSError, ValueError):
            pass
        omnirig.release_all()    # else OmniRig keeps running and polling the radio
        sdrconsole.release_all()    # close the SDR Console COM port
        os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="winSync: keep a KiwiSDR or WebSDR tuned to Thetis (TCP/IP CAT), "
                                             "a radio through OmniRig, or SDR Console")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--follow", choices=("auto", "A", "B"),
                    help="VFO to follow (overrides [sync] follow in the config)")
    ap.add_argument("--control", choices=("radio", "kiwi", "both"),
                    help="two-way sync: which side tunes the other (overrides [sync] control)")
    ap.add_argument("--source", choices=("thetis", "omnirig", "sdrconsole"),
                    help="where the radio's state comes from (overrides [cat] source)")
    ap.add_argument("--rig", type=int, choices=(1, 2),
                    help="OmniRig rig number (overrides [omnirig] rig)")
    ap.add_argument("--dry-run", action="store_true",
                    help="testing: two-way sync decides as usual but never sends anything to the radio")
    ap.add_argument("--diag", action="store_true",
                    help="testing: the page logs every Kiwi readback change and tune to this log")
    ap.add_argument("--exit-with-stdin", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.exit_with_stdin:
        exit_when_parent_goes()
    cfg = load_config(args.config)
    if args.follow:
        cfg["sync"]["follow"] = args.follow
    if args.control:
        cfg["sync"]["control"] = args.control
    if args.source:
        cfg["cat"]["source"] = args.source
    if args.rig:
        cfg["omnirig"]["rig"] = args.rig
    cfg["sync"]["dry_run"] = cfg["sync"].get("dry_run", False) or args.dry_run
    cfg["sync"]["diag"] = cfg["sync"].get("diag", False) or args.diag
    if cfg["sync"]["dry_run"]:
        print("winSync: DRY RUN - nothing will be sent to the radio")
    s = cfg["server"]
    print(f"winSync: open http://{s['host']}:{s['port']}/_winsync/")
    web.run_app(build_app(cfg), host=s["host"], port=s["port"], print=None)


if __name__ == "__main__":
    main()
