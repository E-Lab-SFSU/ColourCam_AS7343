# as7343_plot.py
# Live bar-graph plot of AS7343 channels with reflectance + transmission modes,
# plus %T display in ABS_TX (absorbance/transmission) mode.

import sys, time, math
import numpy as np
import qwiic_as7343
import matplotlib
matplotlib.use("TkAgg")  # GUI backend suitable for Pi
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

LABELS = ["F1\n(405nm)","F2\n(425nm)","FZ\n(450nm)","F3\n(475nm)","F4\n(515nm)",
          "FY\n(550nm)","F5\n(555nm)","FXL\n(600nm)","F6\n(640nm)","F7\n(690nm)",
          "F8\n(745nm)","VIS\n(396-\n766nm)","NIR\n(855nm)"]

# ---- Sensitivity knobs ----
GAIN_CHOICE = "kAgain32"   # try: kAgain128, kAgain256, kAgain512
USE_LED     = True
LED_DRIVE   = 1
SAMPLES     = 1
ALPHA       = 0.30
UPDATE_MS   = 100

# ---- Display/calibration ----
MODE = "RAW"               # "RAW", "REFLECTANCE", "ABSORBANCE", "TRANS", "ABS_TX"
EPS  = 1e-9
dark_ref   = None
white_ref  = None
blank_ref  = None
LED_IS_ON  = False

# ---- Optional timing knobs for better dark capture ----
DARK_LED_SETTLE_MS  = 800
DARK_FLUSH_FRAMES   = 2
WHITE_LED_SETTLE_MS = 150

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

def apply_calibration(vals):
    """
    RAW:         raw counts (no dark/white/blank)
    REFLECTANCE: R = (S-D)/(W-D)
    ABSORBANCE:  A* = -log10(R)        [reflectance/log view]
    TRANS:       T = (I-D)/(I0-D)      [transmittance 0..1]
    ABS_TX:      A = log10((I0-D)/(I-D))  [true transmission absorbance]
    """
    global dark_ref, white_ref, blank_ref, MODE
    if MODE == "RAW":
        return vals
    if MODE in ("REFLECTANCE", "ABSORBANCE"):
        if dark_ref is not None:
            vals = [max(v - d, EPS) for v, d in zip(vals, dark_ref)]
        if white_ref is None:
            return vals
        w = [max(wv - (dark_ref[i] if dark_ref else 0), EPS) for i, wv in enumerate(white_ref)]
        R = [v / wv for v, wv in zip(vals, w)]
        if MODE == "REFLECTANCE":
            return R
        else:
            return [ -math.log10(max(r, EPS)) for r in R ]
    if MODE in ("TRANS", "ABS_TX") and blank_ref is not None:
        I  = [max(v  - (dark_ref[i] if dark_ref else 0), EPS) for i, v in enumerate(vals)]
        I0 = [max(bv - (dark_ref[i] if dark_ref else 0), EPS) for i, bv in enumerate(blank_ref)]
        if MODE == "TRANS":
            return [ Ik / I0k for Ik, I0k in zip(I, I0) ]
        else:
            return [ math.log10(I0k / Ik) for I0k, Ik in zip(I0, I) ]
    return vals

def main():
    sensor = init_sensor()

    fig, ax = plt.subplots(figsize=(10,4))
    bars = ax.bar(LABELS, [0]*len(LABELS))
    # Secondary y-axis for %T (visible only in ABS_TX)
    A_to_pct = lambda A: 100.0 * (10.0 ** (-np.array(A)))
    pct_to_A = lambda pct: -np.log10(np.array(pct) / 100.0)
    secax = ax.secondary_yaxis('right', functions=(A_to_pct, pct_to_A))
    secax.set_ylabel('%T')
    secax.set_visible(False)

    # %T labels above bars (visible only in ABS_TX)
    pct_labels = [ax.text(b.get_x()+b.get_width()/2, 0, "", ha='center',
                          va='bottom', fontsize=8, rotation=0, visible=False)
                  for b in bars]

    def set_ylim_for_mode():
        if MODE == "RAW":
            ax.set_ylim(0, 500)
        elif MODE == "REFLECTANCE":
            ax.set_ylim(0, 1.2)
        elif MODE == "ABSORBANCE":
            ax.set_ylim(0, 2.0)
        elif MODE == "TRANS":
            ax.set_ylim(0, 1.2)
        else:  # ABS_TX
            ax.set_ylim(-0.2, 2.0)  # allow small negative A to debug blanks
        ax.set_autoscale_on(False)
        secax.set_visible(MODE == "ABS_TX")

    set_ylim_for_mode()
    ax.set_ylabel("Counts / Ratio / A")
    ax.set_title(f"AS7343 Live Channels — Mode: {MODE}")
    plt.tight_layout()

    def on_key(event):
        global dark_ref, white_ref, blank_ref, MODE, LED_IS_ON
        if event.key in ("q", "escape"):
            plt.close(event.canvas.figure)

        elif event.key == "d":
            prev = LED_IS_ON
            led_set(sensor, False); LED_IS_ON = False
            time.sleep(DARK_LED_SETTLE_MS / 1000.0)
            for _ in range(DARK_FLUSH_FRAMES):
                _ = read_frame(sensor); time.sleep(0.01)
            dark_ref = read_values_stacked(sensor, SAMPLES)
            print(f"[cal] Dark captured (LED off {DARK_LED_SETTLE_MS} ms).")
            led_set(sensor, prev); LED_IS_ON = prev

        elif event.key == "w":
            if USE_LED and not LED_IS_ON:
                led_set(sensor, True); LED_IS_ON = True
                time.sleep(WHITE_LED_SETTLE_MS / 1000.0)
            white_ref = read_values_stacked(sensor, SAMPLES)
            print("[cal] White captured (LED on).")

        elif event.key == "b":
            if USE_LED and not LED_IS_ON:
                led_set(sensor, True); LED_IS_ON = True
                time.sleep(WHITE_LED_SETTLE_MS / 1000.0)
            blank_ref = read_values_stacked(sensor, SAMPLES)
            print("[cal] Blank captured (LED on).")

        elif event.key == "m":
            MODE = {
                "RAW":"REFLECTANCE",
                "REFLECTANCE":"ABSORBANCE",
                "ABSORBANCE":"ABS_TX",
                "ABS_TX":"RAW"
            }[MODE]
            print(f"[view] Mode -> {MODE}")
            set_ylim_for_mode()
            ax.figure.canvas.draw_idle()

    fig.canvas.mpl_connect("key_press_event", on_key)

    ema = [0.0]*len(LABELS)

    def fmt_pct(p):
        # Clamp for display; show <0.1% for tiny values
        if p < 0.05: return "<0.1%"
        if p > 999.5: return ">999%"
        return f"{p:.1f}%"

    def update(_frame):
        nonlocal ema
        vals = read_values_stacked(sensor, SAMPLES)
        vals = apply_calibration(vals)

        # EMA smoothing
        ema = [ALPHA*v + (1-ALPHA)*e for v, e in zip(vals, ema)]
        for b, v in zip(bars, ema):
            b.set_height(v)

        # Show %T labels only in ABS_TX (A -> %T)
        if MODE == "ABS_TX":
            pct = A_to_pct(ema)
            for b, t, p in zip(bars, pct_labels, pct):
                t.set_text(fmt_pct(float(p)))
                t.set_position((b.get_x() + b.get_width()/2.0, b.get_height()))
                t.set_visible(True)
            secax.set_visible(True)
        else:
            for t in pct_labels:
                t.set_visible(False)
            secax.set_visible(False)

        ax.set_title(f"AS7343 Live Channels — Mode: {MODE}")
        return bars + pct_labels

    ani = FuncAnimation(fig, update, interval=UPDATE_MS, blit=False)

    try:
        plt.show()
    finally:
        try:
            sensor.spectral_measurement_disable()
            sensor.power_off()
            if USE_LED:
                try: sensor.set_led_off()
                except Exception: pass
        except Exception:
            pass

if __name__ == "__main__":
    main()
