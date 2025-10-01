# as7343_plot_v2.py
# Live bar-graph plot of AS7343 channels with reflectance + transmission modes.

import sys, time, math
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
USE_LED     = True         # set False if your board has no LED
LED_DRIVE   = 1            # higher index = more current (board-dependent)
SAMPLES     = 1            # stack N frames per plotted update (boosts counts & SNR)
ALPHA       = 0.30         # EMA smoothing (0..1); higher = snappier
UPDATE_MS   = 100          # ~10 Hz UI update

# ---- Display/calibration ----
MODE = "RAW"               # "RAW", "REFLECTANCE", "ABSORBANCE", "ABS_TX"
EPS  = 1e-9
dark_ref   = None          # for dark correction
white_ref  = None          # reflectance white reference
blank_ref  = None          # transmission blank (I0)
LED_IS_ON  = False         # track LED state we set

# ---------- LED helpers ----------
def led_set(sensor, on):
    if not USE_LED:
        return
    try:
        if on:
            sensor.set_led_on()
        else:
            sensor.set_led_off()
    except Exception:
        pass

# ---------- Sensor setup ----------
def init_sensor():
    global LED_IS_ON
    s = qwiic_as7343.QwiicAS7343()
    if not s.is_connected() or not s.begin():
        print("AS7343 not detected/failed to begin.", file=sys.stderr); sys.exit(1)
    s.power_on()

    # Gain (robust across driver variants)
    try:
        s.set_a_gain(getattr(s, GAIN_CHOICE))
    except Exception:
        try:
            s.set_a_gain(256)  # some builds accept raw integer
        except Exception:
            pass

    # Optional: integration/exposure (may not exist in all versions)
    for setter, val in (
        ("set_integration_time_us", 20000), # 20 ms if supported
        ("set_measurement_time_ms", 50),    # total frame time if supported
        ("set_atime", 100),                 # coarse
        ("set_astep", 999),                 # fine
    ):
        try:
            getattr(s, setter)(val)
        except Exception:
            pass

    # LED baseline state
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

# ---------- Read one frame ----------
def read_frame(sensor):
    # Channel constants once (fast)
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

    # Values match LABELS order exactly
    return [
        sensor.get_data(F1),
        sensor.get_data(F2),
        sensor.get_data(FZ),
        sensor.get_data(F3),
        sensor.get_data(F4),
        sensor.get_data(FY),
        sensor.get_data(F5),
        sensor.get_data(FXL),
        sensor.get_data(F6),
        sensor.get_data(F7),
        sensor.get_data(F8),
        sensor.get_data(VIS1),
        sensor.get_data(NIR),
    ]

def read_values_stacked(sensor, samples=SAMPLES):
    acc = [0]*len(LABELS)
    for _ in range(max(1, samples)):
        frame = read_frame(sensor)
        acc = [a+v for a, v in zip(acc, frame)]
    return acc

# ---------- Calibration / display pipeline ----------
def apply_calibration(vals):
    """
    RAW:        returns raw counts (no dark/white/blank applied)
    REFLECTANCE: R = (S-D)/(W-D)
    ABSORBANCE:  A* = -log10(R)  [from reflectance, not true transmission]
    ABS_TX:      A  = log10((I0-D)/(I-D))  [true transmission absorbance]
    """
    global dark_ref, white_ref, blank_ref, MODE

    if MODE == "RAW":
        return vals  # truly raw in this version

    # Reflectance modes use white
    if MODE in ("REFLECTANCE", "ABSORBANCE"):
        # dark-correct sample
        if dark_ref is not None:
            vals = [max(v - d, EPS) for v, d in zip(vals, dark_ref)]
        if white_ref is None:
            return vals
        # dark-correct white
        w = [max(wv - (dark_ref[i] if dark_ref else 0), EPS) for i, wv in enumerate(white_ref)]
        R = [v / wv for v, wv in zip(vals, w)]
        if MODE == "REFLECTANCE":
            return R
        else:
            return [max(0.0, -math.log10(max(r, EPS))) for r in R]

    # Transmission absorbance (UV–Vis style) uses blank
    if MODE == "ABS_TX" and blank_ref is not None:
        I  = [max(v  - (dark_ref[i] if dark_ref else 0), EPS) for i, v in enumerate(vals)]
        I0 = [max(bv - (dark_ref[i] if dark_ref else 0), EPS) for i, bv in enumerate(blank_ref)]
        return [max(0.0, math.log10(I0k / Ik)) for I0k, Ik in zip(I0, I)]

    return vals

# ---------- Main / UI ----------
def main():
    sensor = init_sensor()

    fig, ax = plt.subplots(figsize=(10,4))
    bars = ax.bar(LABELS, [0]*len(LABELS))

    def set_ylim_for_mode():
        if MODE == "RAW":
            ax.set_ylim(0, 500)
        elif MODE == "REFLECTANCE":
            ax.set_ylim(0, 1.2)
        elif MODE == "ABSORBANCE":
            ax.set_ylim(0, 2.0)
        else:  # ABS_TX
            ax.set_ylim(0, 2.0)
        ax.set_autoscale_on(False)

    set_ylim_for_mode()
    ax.set_ylabel("Counts / Ratio / A")
    ax.set_title(f"AS7343 Live Channels — Mode: {MODE}")
    plt.tight_layout()

    # Key bindings
    def on_key(event):
        global dark_ref, white_ref, blank_ref, MODE, LED_IS_ON
        if event.key in ("q", "escape"):
            plt.close(event.canvas.figure)

        elif event.key == "d":  # DARK capture (LED off)
            prev = LED_IS_ON
            led_set(sensor, False); LED_IS_ON = False
            time.sleep(0.1)
            vals = read_values_stacked(sensor, SAMPLES)
            dark_ref = vals[:]
            print("[cal] Dark captured (LED off).")
            led_set(sensor, prev); LED_IS_ON = prev

        elif event.key == "w":  # WHITE capture (LED on, reflectance)
            if USE_LED and not LED_IS_ON:
                led_set(sensor, True); LED_IS_ON = True
                time.sleep(0.1)
            vals = read_values_stacked(sensor, SAMPLES)
            white_ref = vals[:]
            print("[cal] White captured (LED on).")

        elif event.key == "b":  # BLANK capture (LED on, transmission)
            if USE_LED and not LED_IS_ON:
                led_set(sensor, True); LED_IS_ON = True
                time.sleep(0.1)
            vals = read_values_stacked(sensor, SAMPLES)
            blank_ref = vals[:]
            print("[cal] Blank captured (LED on).")

        elif event.key == "m":  # cycle modes
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

    def update(_frame):
        nonlocal ema
        vals = read_values_stacked(sensor, SAMPLES)
        vals = apply_calibration(vals)

        # EMA smoothing
        ema = [ALPHA*v + (1-ALPHA)*e for v, e in zip(vals, ema)]
        for b, v in zip(bars, ema):
            b.set_height(v)

        ax.set_title(f"AS7343 Live Channels — Mode: {MODE}")
        return bars

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
