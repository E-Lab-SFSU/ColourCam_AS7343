# as7343_console.py
# Console table for AS7343 that shows percent values per channel
# and lets you toggle REFLECTANCE (%R), ABS_TX (A & %T via Beer–Lambert),
# and ABSORBANCE (A* from reflectance) modes.
#
# Keys:
#   d = capture Dark (LED off)
#   w = capture White (LED on)
#   b = capture Blank I0 (LED on)
#   m = cycle mode (REFLECTANCE -> ABS_TX -> ABSORBANCE -> ...)
#   q = quit
#
# Beer–Lambert (for ABS_TX / UV-Vis comparison):
#   A = log10(I0 / I)
#   %T = 100 * 10^(-A)

import sys, time, math, threading, queue
import numpy as np
import qwiic_as7343

LABELS = [
    "F1 405", "F2 425", "FZ 450", "F3 475", "F4 515",
    "FY 550", "F5 555", "FXL 600", "F6 640", "F7 690",
    "F8 745", "VIS", "NIR 855"
]

# ---- Sensitivity knobs ----
GAIN_CHOICE = "kAgain32"    # try: kAgain128, kAgain256, kAgain512
USE_LED     = True
LED_DRIVE   = 1
SAMPLES     = 1
ALPHA       = 0.30          # EMA smoothing on computed values
UPDATE_MS   = 150

# ---- Display/calibration ----
MODE = "REFLECTANCE"        # "REFLECTANCE", "ABS_TX", "ABSORBANCE"
EPS  = 1e-9
dark_ref   = None
white_ref  = None
blank_ref  = None           # transmission blank (I0) for ABS_TX
LED_IS_ON  = False

# ---- Optional timing knobs for better dark capture ----
DARK_LED_SETTLE_MS  = 800
DARK_FLUSH_FRAMES   = 2
WHITE_LED_SETTLE_MS = 150

# ---- Keyboard input thread ----
_cmd_q = queue.Queue()

def _stdin_reader():
    while True:
        try:
            s = sys.stdin.readline()
        except Exception:
            break
        if not s:
            break
        _cmd_q.put(s.strip().lower())

def led_set(sensor, on):
    if not USE_LED: return
    try:
        sensor.set_led_on() if on else sensor.set_led_off()
    except Exception:
        pass

def init_sensor():
    global LED_IS_ON
    s = qwiic_as7343.QwiicAS7343()
    if not s.is_connected() or not s.begin():
        print("AS7343 not detected/failed to begin.", file=sys.stderr); sys.exit(1)
    s.power_on()
    try:
        s.set_a_gain(getattr(s, GAIN_CHOICE))
    except Exception:
        try: s.set_a_gain(256)
        except Exception: pass
    for setter, val in (
        ("set_integration_time_us", 20000),
        ("set_measurement_time_ms", 50),
        ("set_atime", 100),
        ("set_astep", 999),
    ):
        try: getattr(s, setter)(val)
        except Exception: pass
    if USE_LED:
        try:
            s.set_led_drive(LED_DRIVE)
            s.set_led_on()
            LED_IS_ON = True
        except Exception:
            LED_IS_ON = False
    else:
        LED_IS_ON = False
    if not s.set_auto_smux(s.kAutoSmux18Channels):
        print("Failed to set AutoSMUX=18.", file=sys.stderr); sys.exit(1)
    if not s.spectral_measurement_enable():
        print("Failed to enable spectral measurements.", file=sys.stderr); sys.exit(1)
    return s

def read_frame(sensor):
    F1   = sensor.kChPurpleF1405nm
    F2   = sensor.kChDarkBlueF2425nm
    FZ   = sensor.kChBlueFz450nm
    F3   = sensor.kChLightBlueF3475nm
    F4   = sensor.kChBlueF4515nm
    F5   = sensor.kChGreenF5550nm
    FY   = sensor.kChGreenFy555nm
    FXL  = sensor.kChOrangeFxl600nm
    F6   = sensor.kChBrownF6640nm
    F7   = sensor.kChRedF7690nm
    F8   = sensor.kChDarkRedF8745nm
    VIS1 = sensor.kChVis1
    NIR  = sensor.kChNir855nm
    sensor.read_all_spectral_data()
    return [
        sensor.get_data(F1),  sensor.get_data(F2),  sensor.get_data(FZ),
        sensor.get_data(F3),  sensor.get_data(F4),  sensor.get_data(FY),
        sensor.get_data(F5),  sensor.get_data(FXL), sensor.get_data(F6),
        sensor.get_data(F7),  sensor.get_data(F8),  sensor.get_data(VIS1),
        sensor.get_data(NIR),
    ]

def read_values_stacked(sensor, samples=SAMPLES):
    acc = [0]*len(LABELS)
    for _ in range(max(1, samples)):
        frame = read_frame(sensor)
        acc = [a+v for a, v in zip(acc, frame)]
    return acc

def compute_reflectance(vals):
    """R = (S-D)/(W-D) ; returns R and %R"""
    if dark_ref is not None:
        vals = [max(v - d, EPS) for v, d in zip(vals, dark_ref)]
    if white_ref is None:
        # No white: fall back to raw-ish; avoid div by zero
        return [0.0]*len(vals), [0.0]*len(vals)
    w = [max(wv - (dark_ref[i] if dark_ref else 0), EPS) for i, wv in enumerate(white_ref)]
    R = [max(v / wv, EPS) for v, wv in zip(vals, w)]
    pctR = [min(1e6, r*100.0) for r in R]
    return R, pctR

def compute_abs_tx(vals):
    """Transmission absorbance via Beer–Lambert:
       A = log10((I0-D)/(I-D)),  %T = 100 * 10^(-A)"""
    if blank_ref is None:
        return [0.0]*len(vals), [0.0]*len(vals)
    I  = [max(v  - (dark_ref[i] if dark_ref else 0), EPS) for i, v  in enumerate(vals)]
    I0 = [max(bv - (dark_ref[i] if dark_ref else 0), EPS) for i, bv in enumerate(blank_ref)]
    A  = [math.log10(I0k / Ik) for I0k, Ik in zip(I0, I)]
    pctT = [min(1e6, 100.0 * (10.0 ** (-a))) for a in A]
    return A, pctT

def compute_absorbance_from_reflectance(vals):
    """Log-reflectance view:
       A* = −log10(R). Also report %R for a percent column."""
    R, pctR = compute_reflectance(vals)
    Astar = [-math.log10(max(r, EPS)) for r in R]
    return Astar, pctR

def fmt_pct(p):
    if p < 0.05: return "<0.1"
    if p > 999.5: return ">999"
    return f"{p:6.1f}"

def fmt_abs(a):
    # show 2 decimals, allow small negatives for diagnosis
    return f"{a:5.2f}"

def clear_screen():
    # ANSI clear
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

def print_header():
    print("AS7343 Console — Modes: REFLECTANCE (%R) | ABS_TX (A, %T) | ABSORBANCE (A*, %R)")
    print("Keys: d=Dark  w=White  b=Blank(I0)  m=Mode  q=Quit")
    cal = []
    if dark_ref is not None: cal.append("Dark✓")
    if white_ref is not None: cal.append("White✓")
    if blank_ref is not None: cal.append("Blank✓")
    cal_str = ("  Cal: " + ", ".join(cal)) if cal else "  Cal: (none)"
    led_str = ("LED:on" if LED_IS_ON else "LED:off") if USE_LED else "LED:n/a"
    print(f"Mode: {MODE}   {led_str}{cal_str}")
    if MODE == "ABS_TX":
        print("Beer–Lambert active: A = log10(I0/I),  %T = 100·10^(−A)")
    print("-"*64)

def print_table(rows, mode):
    if mode == "REFLECTANCE":
        print(f"{'Chan':<8} {'%R':>8}")
        for name, pctR in rows:
            print(f"{name:<8} {fmt_pct(pctR):>8}")
    elif mode == "ABS_TX":
        print(f"{'Chan':<8} {'A':>6} {'%T':>8}")
        for name, (A, pctT) in rows:
            print(f"{name:<8} {fmt_abs(A):>6} {fmt_pct(pctT):>8}")
    else:  # ABSORBANCE (log-reflectance)
        print(f"{'Chan':<8} {'A*':>6} {'%R':>8}")
        for name, (Astar, pctR) in rows:
            print(f"{name:<8} {fmt_abs(Astar):>6} {fmt_pct(pctR):>8}")
    print("-"*64)
    print("Tip: capture Dark (d) first, then White (w) for reflectance; for UV-Vis style,"
          " capture Blank I0 (b) and use ABS_TX.")

def main():
    global dark_ref, white_ref, blank_ref, MODE, LED_IS_ON
    sensor = init_sensor()

    # start stdin reader
    t = threading.Thread(target=_stdin_reader, daemon=True)
    t.start()

    ema_vals = None
    try:
        while True:
            # handle commands (non-blocking)
            while not _cmd_q.empty():
                cmd = _cmd_q.get()
                if cmd == "q":
                    raise KeyboardInterrupt
                elif cmd == "m":
                    MODE = {"REFLECTANCE":"ABS_TX", "ABS_TX":"ABSORBANCE", "ABSORBANCE":"REFLECTANCE"}[MODE]
                elif cmd == "d":
                    prev = LED_IS_ON
                    led_set(sensor, False); LED_IS_ON = False
                    time.sleep(DARK_LED_SETTLE_MS / 1000.0)
                    for _ in range(DARK_FLUSH_FRAMES):
                        _ = read_frame(sensor); time.sleep(0.01)
                    dark_ref = read_values_stacked(sensor, SAMPLES)
                elif cmd == "w":
                    if USE_LED and not LED_IS_ON:
                        led_set(sensor, True); LED_IS_ON = True
                        time.sleep(WHITE_LED_SETTLE_MS / 1000.0)
                    white_ref = read_values_stacked(sensor, SAMPLES)
                elif cmd == "b":
                    if USE_LED and not LED_IS_ON:
                        led_set(sensor, True); LED_IS_ON = True
                        time.sleep(WHITE_LED_SETTLE_MS / 1000.0)
                    blank_ref = read_values_stacked(sensor, SAMPLES)

            vals = read_values_stacked(sensor, SAMPLES)

            # smooth raw counts to reduce flicker before transforms
            if ema_vals is None:
                ema_vals = vals[:]
            else:
                ema_vals = [ALPHA*v + (1-ALPHA)*e for v, e in zip(vals, ema_vals)]

            # compute by mode
            if MODE == "REFLECTANCE":
                _R, pctR = compute_reflectance(ema_vals)
                rows = list(zip(LABELS, pctR))
            elif MODE == "ABS_TX":
                A, pctT = compute_abs_tx(ema_vals)
                rows = list(zip(LABELS, zip(A, pctT)))
            else:  # ABSORBANCE (log-reflectance)
                Astar, pctR = compute_absorbance_from_reflectance(ema_vals)
                rows = list(zip(LABELS, zip(Astar, pctR)))

            # draw
            clear_screen()
            print_header()
            print_table(rows, MODE)

            time.sleep(max(0.01, UPDATE_MS/1000.0))

    except KeyboardInterrupt:
        pass
    finally:
        try:
            sensor.spectral_measurement_disable()
            sensor.power_off()
            if USE_LED:
                try: sensor.set_led_off()
                except Exception: pass
        except Exception:
            pass
        print("\nBye.")

if __name__ == "__main__":
    main()
