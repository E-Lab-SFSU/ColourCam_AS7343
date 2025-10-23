#!/usr/bin/env python3
# as7343_wellplate.py
# Per-well blank referencing for a 12‑well plate (A1–C4) using AS7343.
# Capture a blank for each well, then compute Absorbance A = log10(I0/I) and %T = 100*10^(−A)
# with optional dark subtraction. Blanks are saved/loaded to JSON.
#
# Controls (type a command then press Enter):
#   w <WELL>      switch current well (e.g., "w A1", "w b3", "w C4")
#   blank         capture blank for current well (I0 for that well)
#   dark          capture a global dark reference (D)
#   read [n]      read sample once (optionally average n reads)
#   live [Hz]     live read loop (default 2 Hz). Press Enter on an empty line to stop.
#   status        show which wells have blanks + integration/gain
#   save [file]   save blanks (default well_blanks.json)
#   load [file]   load blanks (default well_blanks.json)
#   it <ms>       set integration time in milliseconds (best effort; depends on library support)
#   gain <x>      set gain (e.g., 1, 2, 4, 8, 16, 32, 64); depends on library support
#   help          show this help
#   q             quit
#
# NOTE: You may need to tweak the read_channels() function depending on your earlier code.
# It currently tries a few common SparkFun API methods and falls back with guidance if missing.
#
# Requires: pip install sparkfun-qwiic-as7343 (or your existing qwiic_as7343 install)
#
# Author: Color Cam
# Date: 2025-10-23
#
# MIT License

import sys, os, time, math, json, threading, datetime
from collections import OrderedDict

# Try to import the sensor lib
try:
    import qwiic_as7343
except Exception as e:
    print("ERROR: Could not import qwiic_as7343. Install with:")
    print("  pip3 install sparkfun-qwiic-as7343")
    print("Or use your existing environment. Exception:\n", e)
    sys.exit(1)

# ----- Configuration -----
WELLS = [f"{r}{c}" for r in ["A","B","C"] for c in [1,2,3,4]]  # 12 wells A1..C4
# Channel labels matching typical AS7343 mapping used earlier
LABELS = ["F1 (405)","F2 (425)","FZ (450)","F3 (475)","F4 (515)",
          "FY (550)","F5 (555)","FXL (600)","F6 (640)","F7 (690)",
          "F8 (745)","VIS (broad)","NIR (855)"]
NUM_CH = len(LABELS)

DEFAULT_JSON = "well_blanks.json"
EPS = 1.0  # counts floor after dark subtraction to avoid log/div-by-zero
DEFAULT_AVG = 3  # number of readings to average for one sample/blank capture
LIVE_DEFAULT_HZ = 2.0

# ----- Utility: pretty timestamp -----
def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")

# ----- Sensor init -----
def init_sensor():
    s = qwiic_as7343.QwiicAS7343()
    if not s.connected:
        print("ERROR: AS7343 not detected on I2C. Check wiring and I2C address.")
        sys.exit(2)
    if not s.begin():
        print("ERROR: AS7343 begin() failed.")
        sys.exit(3)
    # Make a best-effort default configuration.
    try:
        # Not all libraries expose these; wrapped in try/except.
        s.set_integration_time_ms(100)  # 100 ms
    except Exception:
        pass
    try:
        s.set_gain(16)  # moderate gain
    except Exception:
        pass
    return s

# ----- Read channels with averaging -----
def _single_read(sensor):
    """
    Return a list of NUM_CH raw (or calibrated) counts.
    This function tries a few API variants seen in qwiic_as7343 examples.
    Adjust this to match your previous working read path.
    """
    # Variant 1: direct spectral data dict
    try:
        data = sensor.get_calibrated_spectral_data()
        # Expect keys in the sensor's order; convert to list by index.
        # If it's a dict with fixed ordering, try to extract in the expected order.
        if isinstance(data, dict):
            vals = list(data.values())
            if len(vals) >= NUM_CH:
                return vals[:NUM_CH]
        elif isinstance(data, (list, tuple)) and len(data) >= NUM_CH:
            return list(data[:NUM_CH])
    except Exception:
        pass

    # Variant 2: get_all_channels() returning list/tuple
    try:
        vals = sensor.get_all_channels()
        if isinstance(vals, (list, tuple)) and len(vals) >= NUM_CH:
            return list(vals[:NUM_CH])
    except Exception:
        pass

    # Variant 3: read channels one by one
    # Some libs expose .channels or methods like get_channel(n). We try a generic approach.
    try:
        vals = []
        for ch in range(NUM_CH):
            # Replace this with your per-channel accessor if available.
            val = sensor.get_channel(ch)  # may raise AttributeError
            vals.append(float(val))
        if len(vals) == NUM_CH:
            return vals
    except Exception:
        pass

    raise RuntimeError(
        "Could not read channels with the current qwiic_as7343 API.\n"
        "Please edit _single_read() to use the same method you used in your previous code "
        "(e.g., sensor.get_all_channels() or sensor.get_calibrated_spectral_data())."
    )

def read_channels(sensor, averages=DEFAULT_AVG, settle_ms=0):
    """
    Average 'averages' readings. Optionally wait settle_ms between reads.
    Returns list[NUM_CH] of floats.
    """
    acc = [0.0]*NUM_CH
    for i in range(averages):
        vals = _single_read(sensor)
        for k, v in enumerate(vals):
            acc[k] += float(v)
        if settle_ms > 0 and i < (averages - 1):
            time.sleep(settle_ms/1000.0)
    return [x/averages for x in acc]

# ----- Math: dark subtraction, A and %T -----
def compute_absorbance_and_transmittance(sample, blank, dark=None, eps=EPS):
    """
    sample, blank, dark: lists of length NUM_CH
    Returns (A, T_percent) as two lists.
    Using A = log10( (blank - dark) / (sample - dark) ) with EPS floor.
    """
    A = []
    T = []
    for i in range(NUM_CH):
        I  = sample[i] - (dark[i] if (dark is not None) else 0.0)
        I0 = blank[i]  - (dark[i] if (dark is not None) else 0.0)
        if I  < eps: I  = eps
        if I0 < eps: I0 = eps
        a = math.log10(I0 / I)
        t = 100.0 * (10.0 ** (-a))  # = 100 * (I / I0)
        A.append(a)
        T.append(t)
    return A, T

# ----- Persistence -----
def save_json(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path

def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

# ----- TUI helpers -----
def print_header():
    print("\nAS7343 12‑Well Plate — Per‑Well Blanks")
    print("Current commands: w <WELL> | blank | dark | read [n] | live [Hz] | status | save [f] | load [f] | it <ms> | gain <x> | help | q")

def pprint_vector(name, vec, idxs=None, width=10, precision=3):
    if idxs is None:
        idxs = range(NUM_CH)
    # One-line compact print
    parts = []
    for i in idxs:
        parts.append(f"{LABELS[i]}={vec[i]:.{precision}f}")
    print(f"{name}: " + " | ".join(parts))

def summarize(A, T):
    # Highlight a few wavelengths user cares about (if present)
    idx_by_label = {lbl.split()[0]: i for i, lbl in enumerate(LABELS)}  # keys like F5, FXL, F6
    focus = []
    for key in ["F5 (555)","FXL (600)","F6 (640)"]:
        # match by startswith to be robust
        found = None
        for i, lbl in enumerate(LABELS):
            if lbl.startswith(key.split()[0]):
                found = i; break
        if found is not None:
            focus.append(found)
    if focus:
        pprint_vector("A (focus)", A, idxs=focus, precision=3)
        pprint_vector("%T (focus)", T, idxs=focus, precision=1)
    else:
        # fallback show first few
        pprint_vector("A", A[:5], idxs=range(5), precision=3)
        pprint_vector("%T", T[:5], idxs=range(5), precision=1)

# ----- Main run -----
def main():
    sensor = init_sensor()
    print_header()

    # State
    current_well = "A1"
    blanks = OrderedDict((w, None) for w in WELLS)  # map well -> {"I0":[...], "timestamp": str}
    dark = None
    integration_ms = 100
    gain = 16

    print(f"Selected well: {current_well}")
    print("Tip: Take a 'dark' first (cover sensor), then 'blank' for each well.")

    while True:
        try:
            cmd = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not cmd:
            continue

        toks = cmd.split()
        c = toks[0].lower()

        if c in ("q","quit","exit"):
            break

        elif c in ("help","h","?"):
            print_header()
            continue

        elif c in ("w","well"):
            if len(toks) < 2:
                print("Usage: w <WELL>  (e.g., w A1)")
                continue
            w = toks[1].upper()
            if w not in WELLS:
                print(f"Invalid well '{w}'. Valid: {', '.join(WELLS)}")
                continue
            current_well = w
            print(f"Selected well: {current_well}")

        elif c == "status":
            have = [w for w,v in blanks.items() if v is not None]
            missing = [w for w,v in blanks.items() if v is None]
            print(f"Well with blanks ({len(have)}): {', '.join(have) if have else 'none'}")
            print(f"Wells missing blanks ({len(missing)}): {', '.join(missing) if missing else 'none'}")
            print(f"Dark: {'set' if dark is not None else 'not set'}")
            print(f"Integration ~{integration_ms} ms, Gain ~{gain}x")

        elif c == "blank":
            print(f"Capturing blank for {current_well} (averaging {DEFAULT_AVG} reads)...")
            I0 = read_channels(sensor, averages=DEFAULT_AVG, settle_ms=0)
            blanks[current_well] = {"I0": I0, "timestamp": now_iso()}
            print(f"Blank stored for {current_well} @ {blanks[current_well]['timestamp']}")
            pprint_vector("I0", I0, precision=1)

        elif c == "dark":
            print(f"Capturing dark (averaging {DEFAULT_AVG} reads)... Please cover the sensor.")
            time.sleep(0.5)
            D = read_channels(sensor, averages=DEFAULT_AVG, settle_ms=0)
            dark = D
            print("Dark stored.")
            pprint_vector("D", D, precision=1)

        elif c == "read":
            avg = DEFAULT_AVG
            if len(toks) >= 2:
                try:
                    avg = max(1, int(toks[1]))
                except Exception:
                    pass
            if blanks[current_well] is None:
                print(f"No blank for {current_well}. Run 'blank' first.")
                continue
            print(f"Reading sample at {current_well} (avg {avg})...")
            I = read_channels(sensor, averages=avg, settle_ms=0)
            A, T = compute_absorbance_and_transmittance(
                sample=I, blank=blanks[current_well]["I0"], dark=dark, eps=EPS
            )
            pprint_vector("Raw I", I, precision=1)
            summarize(A, T)

        elif c == "live":
            hz = LIVE_DEFAULT_HZ
            if len(toks) >= 2:
                try:
                    hz = float(toks[1])
                except Exception:
                    pass
            if hz <= 0: hz = LIVE_DEFAULT_HZ
            if blanks[current_well] is None:
                print(f"No blank for {current_well}. Run 'blank' first.")
                continue
            interval = 1.0 / hz
            print(f"Live reading {hz:.2f} Hz for {current_well}. Press Enter on an empty line to stop.")
            # Run in foreground; check for empty Enter in a non-blocking way via thread flag.
            stop_flag = {"stop": False}
            def stopper():
                try:
                    while True:
                        s = input()
                        if s.strip() == "":
                            stop_flag["stop"] = True
                            break
                except Exception:
                    stop_flag["stop"] = True

            t = threading.Thread(target=stopper, daemon=True)
            t.start()
            while not stop_flag["stop"]:
                I = read_channels(sensor, averages=1, settle_ms=0)
                A, T = compute_absorbance_and_transmittance(
                    sample=I, blank=blanks[current_well]["I0"], dark=dark, eps=EPS
                )
                print(f"\n[{now_iso()}] {current_well}")
                summarize(A, T)
                time.sleep(interval)

        elif c == "save":
            path = DEFAULT_JSON
            if len(toks) >= 2:
                path = toks[1]
            payload = {
                "timestamp": now_iso(),
                "notes": "AS7343 per-well blanks; dark optional",
                "labels": LABELS,
                "eps": EPS,
                "blanks": blanks,
                "dark": dark,
            }
            save_json(path, payload)
            print(f"Saved to {path}")

        elif c == "load":
            path = DEFAULT_JSON
            if len(toks) >= 2:
                path = toks[1]
            try:
                payload = load_json(path)
                # Basic validation/conversion
                in_blanks = payload.get("blanks", {})
                # ensure all wells exist
                new_blanks = OrderedDict((w, None) for w in WELLS)
                for w, v in in_blanks.items():
                    if w in new_blanks and v is not None:
                        new_blanks[w] = v
                blanks = new_blanks
                dark = payload.get("dark", None)
                print(f"Loaded from {path}.")
            except Exception as e:
                print(f"Failed to load {path}: {e}")

        elif c == "it":
            if len(toks) < 2:
                print("Usage: it <milliseconds>")
                continue
            try:
                ms = int(float(toks[1]))
                try:
                    sensor.set_integration_time_ms(ms)
                    integration_ms = ms
                    print(f"Integration time set to ~{ms} ms")
                except Exception as e:
                    print(f"Your library may not support set_integration_time_ms(): {e}")
            except Exception:
                print("Invalid integration time.")

        elif c == "gain":
            if len(toks) < 2:
                print("Usage: gain <multiplier> (e.g., 1,2,4,8,16,32,64)")
                continue
            try:
                g = int(float(toks[1]))
                try:
                    sensor.set_gain(g)
                    gain = g
                    print(f"Gain set to ~{g}x")
                except Exception as e:
                    print(f"Your library may not support set_gain(): {e}")
            except Exception:
                print("Invalid gain.")
        else:
            print("Unknown command. Type 'help' for options.")

    print("Goodbye.")

if __name__ == "__main__":
    main()
