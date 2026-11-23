"""maxwellctl: command-line control for Audeze Maxwell headsets."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import NoDeviceError, __version__, open_headset
from . import protocol as p
from .headset import HeadsetTimeout, Status
from .protocol import ProtocolError
from .transport import TransportError, list_audeze_nodes, list_devices


def _route(text: str) -> int:
    value = {"headset": p.ROUTE_HEADSET, "dongle": p.ROUTE_LOCAL, "local": p.ROUTE_LOCAL}.get(text.lower())
    if value is None:
        value = int(text, 0)
    return value


def _fmt_status(s: Status) -> str:
    lines = []
    if not s.headset_reachable:
        lines.append("Headset:        not reachable (switched off or out of range?)")
        return "\n".join(lines)
    lines.append(f"Battery:        {s.battery}%")
    if s.mic_muted is not None:
        lines.append(f"Microphone:     {'muted' if s.mic_muted else 'live'}")
    if s.eq_preset is not None:
        lines.append(f"EQ preset:      {s.eq_preset} ({p.EQ_PRESETS.get(s.eq_preset, 'unknown')})")
    if s.noise_filter is not None:
        lines.append(f"Noise filter:   {p.NOISE_FILTER_LEVELS.get(s.noise_filter, s.noise_filter)}")
    if s.sidetone is not None:
        st = s.sidetone
        lines.append(f"Sidetone:       {'on' if st.enabled else 'off'}, level {st.level}/{st.max_level}")
    if s.voice_prompt_level is not None:
        lines.append(f"Voice prompts:  {s.voice_prompt_level}/{p.VOICE_PROMPT_MAX}"
                     + (" (off)" if s.voice_prompt_level == 0 else ""))
    if s.chatmix is not None:
        lines.append(f"Chat mix:       {s.chatmix}/{p.CHATMIX_MAX} ({_chatmix_text(s.chatmix)})")
    lines.append("Auto power-off: " + (
        "unknown" if s.auto_off_minutes is None
        else "never" if s.auto_off_minutes == 0 else f"{s.auto_off_minutes} min"))
    if s.headset_firmware:
        lines.append(f"Headset FW:     {s.headset_firmware}")
    if s.dongle_firmware:
        lines.append(f"Dongle FW:      {s.dongle_firmware}")
    lines.append("ANC mode:       not readable (protocol not known yet, see README)")
    for name, err in s.errors.items():
        lines.append(f"  ! {name}: {err}")
    return "\n".join(lines)


def _chatmix_text(v: int) -> str:
    if v == 10:
        return "balanced"
    return "more game" if v < 10 else "more chat"


def _status_dict(s: Status) -> dict:
    d = {
        "reachable": s.headset_reachable,
        "battery": s.battery,
        "mic_muted": s.mic_muted,
        "eq_preset": s.eq_preset,
        "eq_preset_name": p.EQ_PRESETS.get(s.eq_preset) if s.eq_preset else None,
        "noise_filter": s.noise_filter,
        "sidetone": None if s.sidetone is None else vars(s.sidetone),
        "voice_prompt_level": s.voice_prompt_level,
        "chatmix": s.chatmix,
        "auto_off_minutes": s.auto_off_minutes,
        "headset_firmware": s.headset_firmware,
        "dongle_firmware": s.dongle_firmware,
        "errors": s.errors,
    }
    return d


def _choice(value: str, table: dict, what: str) -> int:
    if value.isdigit():
        n = int(value)
        if n in table:
            return n
    for k, name in table.items():
        if name.lower().replace(" ", "") == value.lower().replace(" ", "").replace("-", ""):
            return k
    raise SystemExit(f"unknown {what} '{value}'. Choose from: "
                     + ", ".join(f"{k}={v}" for k, v in table.items()))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="maxwellctl", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--device", help="hidraw node to use (default: first Maxwell found)")
    ap.add_argument("--route", type=_route,
                    help="'headset' (0x80, through the dongle) or 'dongle'/'local' (0x00). "
                         "Default depends on the product id.")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list detected devices")
    st = sub.add_parser("status", help="show everything that can be read (default)")
    st.add_argument("--json", action="store_true")
    st.add_argument("--firmware", action="store_true", help="also read firmware versions")
    sub.add_parser("battery", help="print battery percent")

    eq = sub.add_parser("eq", help="get/set EQ preset")
    eq.add_argument("preset", nargs="?", help="1-10 or name (e.g. 'bass boost')")

    nf = sub.add_parser("noise-filter", help="get/set AI mic noise filter")
    nf.add_argument("level", nargs="?", help="off|low|high or 0-2")

    sd = sub.add_parser("sidetone", help="get/set sidetone")
    sd.add_argument("value", nargs="?", help="on|off or a level (0..max)")

    vp = sub.add_parser("voice-prompts", help="get/set voice prompt volume")
    vp.add_argument("level", nargs="?", type=int, help=f"0 (off) .. {p.VOICE_PROMPT_MAX}")

    ao = sub.add_parser("auto-off", help="get/set auto power-off")
    ao.add_argument("minutes", nargs="?", type=int, help=f"one of {p.AUTO_OFF_MINUTES} (0 = never)")

    sub.add_parser("chatmix", help="print game/chat mix (set with the wheel on the headset)")
    sub.add_parser("firmware", help="print firmware versions")

    mon = sub.add_parser("monitor", help="print messages the device sends on its own "
                                         "(press headset buttons while this runs)")
    mon.add_argument("--seconds", type=float, default=30.0)

    sc = sub.add_parser("scan", help="EXPERIMENTAL read-only scan of setting ids (for finding ANC)")
    sc.add_argument("--save", help="write results to this JSON file")
    sc.add_argument("--compare", help="compare against an earlier --save file")

    raw = sub.add_parser("raw", help="ADVANCED: send a raw request body and print all replies")
    raw.add_argument("hex", nargs="+", help="request body, e.g. 01 09 22 00")
    raw.add_argument("--listen", type=float, default=1.0)
    raw.add_argument("--yes", action="store_true", help="allow bodies that are not known reads")
    return ap


KNOWN_READS = (p.CMD_READ_KEY, p.CMD_READ_PARAM, p.CMD_BATTERY, p.CMD_FIRMWARE)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "status"

    if cmd == "list":
        devices = list_devices()
        if not devices:
            print("No Maxwell control interface found.")
        for d in devices:
            route = "headset (0x80)" if d.default_route == p.ROUTE_HEADSET else "local (0x00)"
            print(f"{d.path}  {d.vendor_id:04x}:{d.product_id:04x}  {d.model}  "
                  f"[{d.name}] interface {d.interface}, default route {route}"
                  + ("" if d.is_known else "  (unknown product id)"))
        nodes = list_audeze_nodes()
        if nodes:
            print("\nAll Audeze hidraw nodes:")
            for n in nodes:
                print("  " + n)
        return 0

    try:
        headset, info = open_headset(args.device, args.route)
    except (NoDeviceError, TransportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        return _run(cmd, args, headset, info)
    except HeadsetTimeout as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except (ProtocolError, ValueError, TransportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        headset.close()


def _run(cmd, args, h, info) -> int:
    if cmd == "status":
        s = h.status(include_firmware=args.firmware)
        if args.json:
            print(json.dumps({"device": info.model, "path": info.path, **_status_dict(s)}, indent=2))
        else:
            print(f"Device:         {info.model} ({info.path})")
            print(_fmt_status(s))
        return 0 if s.headset_reachable else 3

    if cmd == "battery":
        level = h.battery()
        if level is None:
            print("error: headset refused the battery request", file=sys.stderr)
            return 1
        print(level)
        return 0

    if cmd == "eq":
        if args.preset is None:
            v = h.eq_preset()
            print(f"{v} ({p.EQ_PRESETS.get(v, 'unknown')})")
        else:
            h.set_eq_preset(_choice(args.preset, p.EQ_PRESETS, "preset"))
            v = h.eq_preset()
            print(f"EQ preset now {v} ({p.EQ_PRESETS.get(v, 'unknown')})")
        return 0

    if cmd == "noise-filter":
        if args.level is None:
            print(p.NOISE_FILTER_LEVELS.get(h.noise_filter(), "unknown"))
        else:
            h.set_noise_filter(_choice(args.level, p.NOISE_FILTER_LEVELS, "level"))
            print(f"Noise filter now {p.NOISE_FILTER_LEVELS.get(h.noise_filter(), 'unknown')}")
        return 0

    if cmd == "sidetone":
        if args.value is None:
            st = h.sidetone()
        elif args.value.lower() in ("on", "off"):
            h.set_sidetone_enabled(args.value.lower() == "on")
            st = h.sidetone()
        else:
            h.set_sidetone_level(int(args.value))
            st = h.sidetone()
        print(f"{'on' if st.enabled else 'off'}, level {st.level}/{st.max_level}")
        return 0

    if cmd == "voice-prompts":
        if args.level is not None:
            h.set_voice_prompt_level(args.level)
        print(f"{h.voice_prompt_level()}/{p.VOICE_PROMPT_MAX}")
        return 0

    if cmd == "auto-off":
        if args.minutes is not None:
            h.set_auto_off(args.minutes)
            print(f"Auto power-off set to {'never' if args.minutes == 0 else f'{args.minutes} min'}")
        else:
            v = h.auto_off()
            print("unknown (could not decode the reply)" if v is None
                  else "never" if v == 0 else f"{v} min")
        return 0

    if cmd == "chatmix":
        v = h.chatmix()
        print(f"{v}/{p.CHATMIX_MAX} ({_chatmix_text(v)})")
        return 0

    if cmd == "firmware":
        print(f"Headset: {h.firmware()}")
        if h.route == p.ROUTE_HEADSET:
            print(f"Dongle:  {h.firmware(p.ROUTE_LOCAL)}")
        return 0

    if cmd == "monitor":
        print(f"Listening for {args.seconds:.0f}s on {info.path}. Press buttons / turn wheels on "
              "the headset. Ctrl+C to stop.")
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            for m in h.listen(min(1.0, end - time.monotonic())):
                print(f"{time.strftime('%H:%M:%S')}  {m.describe()}")
        return 0

    if cmd == "scan":
        print("Read-only scan (EXPERIMENTAL). Values are '<status> <value...>'. Takes ~40 s if "
              "most ids don't answer.", file=sys.stderr)
        result = h.scan(progress=lambda s: print(f"\r  reading {s} ", end="", file=sys.stderr))
        print(file=sys.stderr)
        for k, v in result.items():
            print(f"{k}: {v}")
        if args.save:
            with open(args.save, "w") as f:
                json.dump(result, f, indent=2)
            print(f"saved to {args.save}", file=sys.stderr)
        if args.compare:
            with open(args.compare) as f:
                old = json.load(f)
            changed = sorted(set(old) | set(result))
            diffs = [(k, old.get(k), result.get(k)) for k in changed if old.get(k) != result.get(k)]
            print("\nDifferences vs", args.compare)
            for k, a, b in diffs or []:
                print(f"  {k}: {a} -> {b}")
            if not diffs:
                print("  none")
        return 0

    if cmd == "raw":
        body = p.parse_hex(" ".join(args.hex))
        if body[:2] not in KNOWN_READS and not args.yes:
            print("refusing: this is not a known read command and may change headset state. "
                  "Pass --yes if you understand the risk.", file=sys.stderr)
            return 1
        print(f"> request      {body.hex(' ').upper()}  (route 0x{h.route:02X})")
        for m in h.raw(body, listen=args.listen):
            print(f"< {m.describe()}")
        return 0

    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    sys.exit(main())
