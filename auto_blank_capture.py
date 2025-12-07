#!/usr/bin/env python3
"""
Automated Blank Capture for Well Plates
Integrates well location calculator, 3D printer control, and AS7343 sensor.

This script:
1. Loads well plate configuration (corners, rows, columns) from JSON or accepts parameters
2. Calculates all well positions using bilinear interpolation
3. Moves 3D printer to each well position
4. Captures blank measurements using AS7343 sensor
5. Saves all blanks to JSON file compatible with as7343_wellplate.py

Usage Examples:
    # Using a config file:
    python auto_blank_capture.py --config well_config.json
    
    # Using command-line arguments:
    python auto_blank_capture.py --rows 3 --cols 4 \\
        --top-left "1.0,30.0,3.0" --bottom-left "1.0,2.0,3.0" \\
        --top-right "30.0,30.0,3.0" --bottom-right "30.0,2.0,3.0"
    
    # Test mode (no printer movement):
    python auto_blank_capture.py --config well_config.json --dummy-printer

Config File Format (well_config.json):
    {
        "num_rows": 3,
        "num_cols": 4,
        "top_left": {"X": 1.0, "Y": 30.0, "Z": 3.0},
        "bottom_left": {"X": 1.0, "Y": 2.0, "Z": 3.0},
        "top_right": {"X": 30.0, "Y": 30.0, "Z": 3.0},
        "bottom_right": {"X": 30.0, "Y": 2.0, "Z": 3.0}
    }

To create config from well location calculator:
    After using module_well_location_calculator.py, manually create a JSON file
    with the above format, or use the command-line arguments.

Raspberry Pi Setup Requirements:
    1. Enable I2C: sudo raspi-config -> Interface Options -> I2C -> Enable
    2. Add user to groups:
       sudo usermod -a -G i2c,dialout $USER
       (Then log out and log back in, or reboot)
    3. Install dependencies:
       pip3 install pyserial sparkfun-qwiic-as7343
    4. Verify I2C sensor: i2cdetect -y 1
    5. Verify serial port: ls -l /dev/ttyUSB* /dev/ttyACM*

Author: Auto-generated integration
Date: 2025
"""

import sys
import os
import time
import json
import threading
from collections import OrderedDict
from datetime import datetime

# Import GUI library
try:
    import FreeSimpleGUI as sg
except Exception as e:
    print("ERROR: Could not import FreeSimpleGUI. Install with: pip install FreeSimpleGUI")
    print("  GUI mode will not be available, but command-line mode still works.")
    sg = None

# Import serial for printer control
try:
    import serial
    import serial.tools.list_ports
except Exception as e:
    print("ERROR: Could not import serial. Install with: pip install pyserial")
    sys.exit(1)

# Import sensor functions from as7343_wellplate
try:
    import qwiic_as7343
except Exception as e:
    print("ERROR: Could not import qwiic_as7343. Install with:")
    print("  pip3 install sparkfun-qwiic-as7343")
    sys.exit(1)

# Import constants and functions from as7343_wellplate
# We'll import the functions we need by copying key parts
try:
    from as7343_wellplate import (
        WELLS, LABELS, NUM_CH, DEFAULT_JSON, EPS, DEFAULT_AVG,
        init_sensor, read_channels, now_iso, save_json
    )
except ImportError as e:
    print("ERROR: Could not import from as7343_wellplate.py")
    print("  Ensure as7343_wellplate.py is in the same directory")
    print(f"  Error: {e}")
    sys.exit(1)

# Import well location calculator module
try:
    import module_well_location_calculator as well_calc
except ImportError as e:
    print("ERROR: Could not import module_well_location_calculator.py")
    print("  Ensure module_well_location_calculator.py is in the same directory")
    print(f"  Error: {e}")
    sys.exit(1)

# ===== Global Configuration Variables =====
# Set these defaults for your well plate (can be overridden by config file or command-line)
# These use the calculator module's defaults, but can be overridden

def get_default_rows():
    """Get default number of rows from calculator module."""
    return getattr(well_calc, 'WELL_NUMBER_OF_ROWS', 3)

def get_default_cols():
    """Get default number of columns from calculator module."""
    return getattr(well_calc, 'WELL_NUMBER_OF_COLS', 4)

def get_default_corners():
    """Get default corner positions from calculator module."""
    corner_dict = getattr(well_calc, 'CORNER_LOC_DICT', {})
    top_left_key = getattr(well_calc, 'TOP_LEFT_KEY', '-=TOP LEFT=-')
    bottom_left_key = getattr(well_calc, 'BOTTOM_LEFT_KEY', '-=BOTTOM LEFT=-')
    top_right_key = getattr(well_calc, 'TOP_RIGHT_KEY', '-=TOP RIGHT=-')
    bottom_right_key = getattr(well_calc, 'BOTTOM_RIGHT_KEY', '-=BOTTOM RIGHT=-')
    
    default_loc = {"X": 1.0, "Y": 1.0, "Z": 1.0}
    return {
        "top_left": corner_dict.get(top_left_key, default_loc),
        "bottom_left": corner_dict.get(bottom_left_key, default_loc),
        "top_right": corner_dict.get(top_right_key, default_loc),
        "bottom_right": corner_dict.get(bottom_right_key, default_loc)
    }

DEFAULT_NUM_ROWS = get_default_rows()
DEFAULT_NUM_COLS = get_default_cols()
default_corners = get_default_corners()
DEFAULT_TOP_LEFT = default_corners["top_left"]
DEFAULT_BOTTOM_LEFT = default_corners["bottom_left"]
DEFAULT_TOP_RIGHT = default_corners["top_right"]
DEFAULT_BOTTOM_RIGHT = default_corners["bottom_right"]

# ===== Well Location Calculation =====
# Uses calculator module's data structure

def get_corner_positions_from_calculator():
    """
    Extract corner positions from module_well_location_calculator's CORNER_LOC_DICT.
    Returns tuple: (top_left, bottom_left, top_right, bottom_right)
    """
    corner_dict = well_calc.CORNER_LOC_DICT
    top_left_key = well_calc.TOP_LEFT_KEY
    bottom_left_key = well_calc.BOTTOM_LEFT_KEY
    top_right_key = well_calc.TOP_RIGHT_KEY
    bottom_right_key = well_calc.BOTTOM_RIGHT_KEY
    
    default_loc = {"X": 1.0, "Y": 1.0, "Z": 1.0}
    
    top_left = corner_dict.get(top_left_key, default_loc)
    bottom_left = corner_dict.get(bottom_left_key, default_loc)
    top_right = corner_dict.get(top_right_key, default_loc)
    bottom_right = corner_dict.get(bottom_right_key, default_loc)
    
    return top_left, bottom_left, top_right, bottom_right

def calculate_well_positions(top_left, bottom_left, top_right, bottom_right, num_rows, num_cols):
    """
    Calculate all well positions using bilinear interpolation from 4 corners.
    
    Args:
        top_left: dict with keys "X", "Y", "Z"
        bottom_left: dict with keys "X", "Y", "Z"
        top_right: dict with keys "X", "Y", "Z"
        bottom_right: dict with keys "X", "Y", "Z"
        num_rows: number of rows in well plate
        num_cols: number of columns in well plate
    
    Returns:
        dict mapping well names (e.g., "A1", "B2") to {"X": float, "Y": float, "Z": float}
    """
    well_positions = {}
    
    # Convert row letters to numbers (A=0, B=1, C=2, etc.)
    def row_to_index(row_letter):
        return ord(row_letter.upper()) - ord('A')
    
    # Generate well names based on num_rows and num_cols
    # For standard format: A1, A2, ..., B1, B2, etc.
    wells = []
    for r in range(num_rows):
        row_letter = chr(ord('A') + r)
        for c in range(1, num_cols + 1):
            wells.append(f"{row_letter}{c}")
    
    # Extract corner coordinates
    tl_x, tl_y, tl_z = top_left["X"], top_left["Y"], top_left["Z"]
    bl_x, bl_y, bl_z = bottom_left["X"], bottom_left["Y"], bottom_left["Z"]
    tr_x, tr_y, tr_z = top_right["X"], top_right["Y"], top_right["Z"]
    br_x, br_y, br_z = bottom_right["X"], bottom_right["Y"], bottom_right["Z"]
    
    # Calculate positions for each well
    for well in wells:
        row_letter = well[0]
        col_num = int(well[1:])
        
        # Convert to 0-based indices
        row_idx = row_to_index(row_letter)
        col_idx = col_num - 1
        
        # Normalized coordinates (0 to 1)
        u = col_idx / (num_cols - 1) if num_cols > 1 else 0.0
        v = row_idx / (num_rows - 1) if num_rows > 1 else 0.0
        
        # Bilinear interpolation
        # Top edge
        top_x = tl_x + u * (tr_x - tl_x)
        top_y = tl_y + u * (tr_y - tl_y)
        top_z = tl_z + u * (tr_z - tl_z)
        
        # Bottom edge
        bottom_x = bl_x + u * (br_x - bl_x)
        bottom_y = bl_y + u * (br_y - bl_y)
        bottom_z = bl_z + u * (br_z - bl_z)
        
        # Interpolate between top and bottom
        x = top_x + v * (bottom_x - top_x)
        y = top_y + v * (bottom_y - top_y)
        z = top_z + v * (bottom_z - top_z)
        
        well_positions[well] = {"X": round(x, 2), "Y": round(y, 2), "Z": round(z, 2)}
    
    return well_positions


def load_well_config(config_file):
    """
    Load well plate configuration from JSON file.
    
    Expected format:
    {
        "num_rows": 3,
        "num_cols": 4,
        "top_left": {"X": 1.0, "Y": 30.0, "Z": 3.0},
        "bottom_left": {"X": 1.0, "Y": 2.0, "Z": 3.0},
        "top_right": {"X": 30.0, "Y": 30.0, "Z": 3.0},
        "bottom_right": {"X": 30.0, "Y": 2.0, "Z": 3.0}
    }
    """
    with open(config_file, 'r') as f:
        config = json.load(f)
    return config


def save_well_config(config_file, num_rows, num_cols, top_left, bottom_left, top_right, bottom_right):
    """Save well plate configuration to JSON file."""
    config = {
        "num_rows": num_rows,
        "num_cols": num_cols,
        "top_left": top_left,
        "bottom_left": bottom_left,
        "top_right": top_right,
        "bottom_right": bottom_right,
        "timestamp": now_iso()
    }
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"Saved well configuration to {config_file}")


def create_config_from_calculator(output_file="well_config.json"):
    """
    Create config file from well location calculator's current state.
    Uses module_well_location_calculator's CORNER_LOC_DICT and WELL_NUMBER_OF_ROWS/COLS.
    
    Args:
        output_file: Output JSON file path
    
    Returns:
        Path to saved config file
    """
    num_rows = well_calc.WELL_NUMBER_OF_ROWS
    num_cols = well_calc.WELL_NUMBER_OF_COLS
    top_left, bottom_left, top_right, bottom_right = get_corner_positions_from_calculator()
    
    save_well_config(output_file, num_rows, num_cols, top_left, bottom_left, top_right, bottom_right)
    return output_file


# ===== Printer Control =====

def find_serial_port():
    """
    Find and return the first available USB serial port.
    Works on both Windows and Raspberry Pi (Linux).
    """
    ports = serial.tools.list_ports.comports()
    
    # On Raspberry Pi, look for common serial port patterns
    # Try USB serial ports first (ttyUSB*, ttyACM*), then check descriptions
    usb_ports = []
    for port in ports:
        device = port.device
        description = port.description.upper() if port.description else ""
        
        # Check if it's a USB serial port (common on Pi: /dev/ttyUSB0, /dev/ttyACM0)
        is_usb_serial = (
            'ttyUSB' in device or 
            'ttyACM' in device or 
            'USB' in description or
            'Serial' in description or
            'CH340' in description or  # Common USB-to-serial chip
            'FTDI' in description or   # Common USB-to-serial chip
            'CP210' in description      # Common USB-to-serial chip
        )
        
        # Exclude Bluetooth and other non-printer serial ports
        exclude = (
            'BLUETOOTH' in description or
            'ttyAMA0' in device or  # Raspberry Pi GPIO serial (usually not for printer)
            'ttyS0' in device       # Raspberry Pi console serial
        )
        
        if is_usb_serial and not exclude:
            usb_ports.append(port)
    
    if not usb_ports:
        print("No USB serial ports found.")
        print("Available ports:")
        for port in ports:
            print(f"  {port.device} - {port.description}")
        return None
    
    # Try each port to see which one works
    for usb_port in usb_ports:
        try:
            ser = serial.Serial(usb_port.device, 115200, timeout=1)
            ser.close()
            print(f"Selected port: {usb_port.device} - {usb_port.description}")
            return usb_port.device
        except serial.SerialException as e:
            print(f"Failed to connect on {usb_port.device}: {e}")
        except PermissionError as e:
            print(f"Permission denied for {usb_port.device}")
            print("  Tip: Add user to 'dialout' group: sudo usermod -a -G dialout $USER")
            print("  Then log out and log back in.")
    
    print("No available ports responded.")
    return None


def wait_for_connection(serial_port):
    """
    Attempt to open a serial connection and wait until it is established.
    Handles permission errors common on Raspberry Pi.
    """
    baud_rate = 115200
    max_attempts = 10
    attempt = 0
    
    while attempt < max_attempts:
        try:
            ser = serial.Serial(serial_port, baud_rate, timeout=1)
            print(f"Connected to {serial_port} at {baud_rate} baud.")
            return ser
        except serial.SerialException as e:
            attempt += 1
            if attempt >= max_attempts:
                print(f"Failed to connect after {max_attempts} attempts.")
                raise
            print(f"Waiting for connection on {serial_port}... (attempt {attempt}/{max_attempts})")
            time.sleep(2)
        except PermissionError as e:
            print(f"Permission denied for {serial_port}")
            print("  Tip: Add user to 'dialout' group: sudo usermod -a -G dialout $USER")
            print("  Then log out and log back in, or reboot.")
            raise


def send_gcode(ser, command):
    """
    Send G-code command to printer and wait for acknowledgment.
    Based on XYZGUI.py send_gcode function.
    """
    ser.write((command + '\n').encode('utf-8'))
    time.sleep(0.1)  # Initial delay for command processing
    while True:
        if ser.in_waiting > 0:
            try:
                raw_data = ser.readline()
                try:
                    response = raw_data.decode('utf-8').strip()
                except UnicodeDecodeError:
                    response = raw_data.decode('latin-1', errors='ignore').strip()
                
                if "ok" in response.lower():
                    break
                elif "error" in response.lower():
                    print(f"Error from printer: {response}")
                    break
            except Exception:
                continue
        time.sleep(0.01)  # Small delay to avoid busy waiting


def move_to_position(ser, x, y, z, feedrate=3000, wait_for_complete=True):
    """
    Move printer to specified X, Y, Z position and wait for movement to complete.
    
    Args:
        ser: serial.Serial object
        x, y, z: target coordinates
        feedrate: movement speed (mm/min)
        wait_for_complete: If True, wait for printer to actually reach position using M400
    """
    command = f"G1 X{x:.2f} Y{y:.2f} Z{z:.2f} F{feedrate}"
    print(f"Moving to: X={x:.2f}, Y={y:.2f}, Z={z:.2f}")
    send_gcode(ser, command)  # This waits for "ok" (command queued)
    
    # Now wait for the actual movement to complete using M400
    # M400 waits for all moves in the queue to complete before sending "ok"
    if wait_for_complete:
        send_gcode(ser, "M400")  # Wait for all moves to complete


# ===== Main Automation =====

def capture_blanks_automated(config_file=None, num_rows=None, num_cols=None,
                             top_left=None, bottom_left=None, top_right=None, bottom_right=None,
                             output_file="well_blanks.json", settle_time=1.0, use_dummy_printer=False,
                             gui_window=None, gui_progress_key=None, gui_status_key=None, gui_stop_flag=None,
                             dark_reference=None):
    """
    Main automation function to capture blanks for all wells.
    
    Args:
        config_file: Path to JSON file with well configuration (optional)
        num_rows, num_cols: Well plate dimensions (if not loading from file)
        top_left, bottom_left, top_right, bottom_right: Corner positions (if not loading from file)
        output_file: Output JSON file for blanks
        settle_time: Time to wait after moving to well before capturing (seconds)
        use_dummy_printer: If True, skip actual printer movement (for testing)
        gui_window: FreeSimpleGUI window object for updates (optional)
        gui_progress_key: Key for progress bar element (optional)
        gui_status_key: Key for status text element (optional)
        gui_stop_flag: Dictionary with 'stop' key to check for user cancellation (optional)
    """
    print("=" * 60)
    print("Automated Blank Capture for Well Plates")
    print("=" * 60)
    
    # Load configuration
    if config_file and os.path.exists(config_file):
        print(f"Loading configuration from {config_file}...")
        config = load_well_config(config_file)
        num_rows = config["num_rows"]
        num_cols = config["num_cols"]
        top_left = config["top_left"]
        bottom_left = config["bottom_left"]
        top_right = config["top_right"]
        bottom_right = config["bottom_right"]
    elif all([num_rows, num_cols, top_left, bottom_left, top_right, bottom_right]):
        print("Using provided configuration...")
    else:
        # Use defaults - try calculator module first, then fallback to defaults
        print("Using default configuration...")
        if not num_rows:
            num_rows = well_calc.WELL_NUMBER_OF_ROWS if hasattr(well_calc, 'WELL_NUMBER_OF_ROWS') else DEFAULT_NUM_ROWS
        if not num_cols:
            num_cols = well_calc.WELL_NUMBER_OF_COLS if hasattr(well_calc, 'WELL_NUMBER_OF_COLS') else DEFAULT_NUM_COLS
        
        # Get corners from calculator module if not provided
        if not all([top_left, bottom_left, top_right, bottom_right]):
            calc_top_left, calc_bottom_left, calc_top_right, calc_bottom_right = get_corner_positions_from_calculator()
            top_left = top_left or calc_top_left
            bottom_left = bottom_left or calc_bottom_left
            top_right = top_right or calc_top_right
            bottom_right = bottom_right or calc_bottom_right
        
        # Final fallback to hardcoded defaults
        top_left = top_left or DEFAULT_TOP_LEFT
        bottom_left = bottom_left or DEFAULT_BOTTOM_LEFT
        top_right = top_right or DEFAULT_TOP_RIGHT
        bottom_right = bottom_right or DEFAULT_BOTTOM_RIGHT
        
        print(f"  Rows: {num_rows}, Columns: {num_cols}")
        print(f"  (Using values from module_well_location_calculator or defaults)")
    
    print(f"Well plate: {num_rows} rows × {num_cols} columns")
    print(f"Corners:")
    print(f"  Top-Left:     X={top_left['X']:.2f}, Y={top_left['Y']:.2f}, Z={top_left['Z']:.2f}")
    print(f"  Bottom-Left:  X={bottom_left['X']:.2f}, Y={bottom_left['Y']:.2f}, Z={bottom_left['Z']:.2f}")
    print(f"  Top-Right:    X={top_right['X']:.2f}, Y={top_right['Y']:.2f}, Z={top_right['Z']:.2f}")
    print(f"  Bottom-Right: X={bottom_right['X']:.2f}, Y={bottom_right['Y']:.2f}, Z={bottom_right['Z']:.2f}")
    
    # Calculate well positions
    print("\nCalculating well positions...")
    well_positions = calculate_well_positions(
        top_left, bottom_left, top_right, bottom_right, num_rows, num_cols
    )
    
    # Generate well list in order (A1, A2, ..., B1, B2, ...)
    wells = []
    for r in range(num_rows):
        row_letter = chr(ord('A') + r)
        for c in range(1, num_cols + 1):
            wells.append(f"{row_letter}{c}")
    
    print(f"Calculated positions for {len(wells)} wells")
    
    # Initialize sensor
    print("\nInitializing AS7343 sensor...")
    try:
        sensor = init_sensor()
        print("Sensor initialized successfully")
    except Exception as e:
        print(f"ERROR: Failed to initialize AS7343 sensor: {e}")
        print("  Tip: Ensure I2C is enabled: sudo raspi-config")
        print("  Tip: Add user to 'i2c' group: sudo usermod -a -G i2c $USER")
        print("  Tip: Check sensor connection: i2cdetect -y 1")
        raise
    
    # Initialize printer (if not using dummy)
    ser = None
    if not use_dummy_printer:
        print("\nConnecting to 3D printer...")
        serial_port = find_serial_port()
        if serial_port:
            ser = wait_for_connection(serial_port)
            print("Printer connected")
        else:
            print("WARNING: Could not connect to printer. Continuing with dummy mode.")
            use_dummy_printer = True
    
    # Initialize blanks dictionary
    blanks = OrderedDict((w, None) for w in wells)
    dark = dark_reference  # Use provided dark reference if available
    
    # Optional: Capture dark reference first (command-line mode only)
    if dark is None and gui_window is None:
        print("\n" + "=" * 60)
        response = input("Capture dark reference first? (y/n): ").strip().lower()
        if response == 'y':
            print("Please cover the sensor, then press Enter...")
            input()
            print("Capturing dark reference...")
            dark = read_channels(sensor, averages=DEFAULT_AVG, settle_ms=0)
            print("Dark reference captured")
    
    # Capture blanks for each well
    print("\n" + "=" * 60)
    print("Starting automated blank capture...")
    print("=" * 60)
    
    # Track previous position to detect row vs column changes
    prev_pos = None
    
    for i, well in enumerate(wells, 1):
        # Check for stop flag from GUI
        if gui_stop_flag and gui_stop_flag.get('stop', False):
            print("\nCapture cancelled by user.")
            if gui_status_key:
                gui_window[gui_status_key].update("Capture cancelled by user.")
                gui_window.refresh()
            break
        
        print(f"\n[{i}/{len(wells)}] Processing well: {well}")
        
        # Update GUI progress
        if gui_window and gui_progress_key:
            progress = int((i / len(wells)) * 100)
            gui_window[gui_progress_key].update(progress)
            gui_window.refresh()
        
        # Get well position
        pos = well_positions[well]
        print(f"  Position: X={pos['X']:.2f}, Y={pos['Y']:.2f}, Z={pos['Z']:.2f}")
        
        # Detect if this is a row change (Y changes) or column change (X changes)
        is_row_change = False
        is_column_change = False
        if prev_pos:
            # Check if Y changed significantly (row change)
            if abs(pos['Y'] - prev_pos['Y']) > 0.1:
                is_row_change = True
            # Check if X changed significantly (column change)
            if abs(pos['X'] - prev_pos['X']) > 0.1:
                is_column_change = True
        
        # Update GUI status and current well
        if gui_window:
            if gui_status_key:
                status_msg = f"Processing well {well} ({i}/{len(wells)})"
                gui_window[gui_status_key].update(status_msg)
            # Update current well display
            current_well_key = "-CURRENT_WELL-"
            if current_well_key in gui_window.AllKeysDict:
                gui_window[current_well_key].update(f"Current Well: {well} - Position: X={pos['X']:.2f}, Y={pos['Y']:.2f}, Z={pos['Z']:.2f}")
            gui_window.refresh()
        
        # Move printer to well position
        if not use_dummy_printer and ser:
            move_to_position(ser, pos['X'], pos['Y'], pos['Z'])
            
            # Always apply settle time, with indication of movement type
            if is_row_change:
                print(f"  Row change detected - Waiting {settle_time}s for settling...")
            elif is_column_change:
                print(f"  Column change detected - Waiting {settle_time}s for settling...")
            else:
                print(f"  Waiting {settle_time}s for settling...")
            
            time.sleep(settle_time)
        else:
            print("  [DUMMY MODE] Skipping printer movement")
            time.sleep(0.5)  # Small delay even in dummy mode
        
        # Update previous position for next iteration
        prev_pos = pos.copy()
        
        # Capture blank
        print(f"  Capturing blank (averaging {DEFAULT_AVG} reads)...")
        if gui_status_key:
            gui_window[gui_status_key].update(f"Capturing blank for {well}...")
            gui_window.refresh()
        I0 = read_channels(sensor, averages=DEFAULT_AVG, settle_ms=0)
        blanks[well] = {"I0": I0, "timestamp": now_iso()}
        print(f"  ✓ Blank captured for {well}")
    
    # Save blanks to file
    print("\n" + "=" * 60)
    print("Saving blanks to file...")
    payload = {
        "timestamp": now_iso(),
        "notes": "AS7343 per-well blanks captured automatically",
        "labels": LABELS,
        "eps": EPS,
        "blanks": blanks,
        "dark": dark,
        "well_config": {
            "num_rows": num_rows,
            "num_cols": num_cols,
            "well_positions": well_positions
        }
    }
    save_json(output_file, payload)
    print(f"✓ Saved blanks to {output_file}")
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  Total wells processed: {len(wells)}")
    print(f"  Blanks captured: {sum(1 for v in blanks.values() if v is not None)}")
    print(f"  Dark reference: {'Yes' if dark else 'No'}")
    print(f"  Output file: {output_file}")
    print("=" * 60)
    
    # Cleanup
    if ser:
        ser.close()
        print("\nPrinter connection closed")


# ===== GUI Functions =====

def create_gui_layout():
    """Create the FreeSimpleGUI layout for automated blank capture."""
    layout = [
        [sg.Text("Automated Blank Capture for Well Plates", font=("Helvetica", 16, "bold"))],
        [sg.HorizontalSeparator()],
        
        # Configuration Section
        [sg.Frame("Well Plate Configuration", [
            [sg.Text("Rows:"), sg.Input(str(DEFAULT_NUM_ROWS), size=(5, 1), key="-ROWS-"),
             sg.Text("Columns:"), sg.Input(str(DEFAULT_NUM_COLS), size=(5, 1), key="-COLS-")],
            [sg.Text("Or load from config file:"), sg.Input(key="-CONFIG_FILE-", size=(30, 1)),
             sg.FileBrowse("Browse", file_types=(("JSON Files", "*.json"),))],
        ])],
        
        [sg.Frame("Corner Positions (mm)", [
            [sg.Text("Top-Left:"), sg.Input(str(DEFAULT_TOP_LEFT["X"]), size=(8, 1), key="-TL_X-"),
             sg.Input(str(DEFAULT_TOP_LEFT["Y"]), size=(8, 1), key="-TL_Y-"),
             sg.Input(str(DEFAULT_TOP_LEFT["Z"]), size=(8, 1), key="-TL_Z-")],
            [sg.Text("Bottom-Left:"), sg.Input(str(DEFAULT_BOTTOM_LEFT["X"]), size=(8, 1), key="-BL_X-"),
             sg.Input(str(DEFAULT_BOTTOM_LEFT["Y"]), size=(8, 1), key="-BL_Y-"),
             sg.Input(str(DEFAULT_BOTTOM_LEFT["Z"]), size=(8, 1), key="-BL_Z-")],
            [sg.Text("Top-Right:"), sg.Input(str(DEFAULT_TOP_RIGHT["X"]), size=(8, 1), key="-TR_X-"),
             sg.Input(str(DEFAULT_TOP_RIGHT["Y"]), size=(8, 1), key="-TR_Y-"),
             sg.Input(str(DEFAULT_TOP_RIGHT["Z"]), size=(8, 1), key="-TR_Z-")],
            [sg.Text("Bottom-Right:"), sg.Input(str(DEFAULT_BOTTOM_RIGHT["X"]), size=(8, 1), key="-BR_X-"),
             sg.Input(str(DEFAULT_BOTTOM_RIGHT["Y"]), size=(8, 1), key="-BR_Y-"),
             sg.Input(str(DEFAULT_BOTTOM_RIGHT["Z"]), size=(8, 1), key="-BR_Z-")],
        ])],
        
        [sg.Frame("Settings", [
            [sg.Text("Output File:"), sg.Input("well_blanks.json", size=(30, 1), key="-OUTPUT-")],
            [sg.Text("Settle Time (s):"), sg.Input("1.0", size=(8, 1), key="-SETTLE-"),
             sg.Checkbox("Dummy Printer Mode", key="-DUMMY-", default=False)],
            [sg.Checkbox("Capture Dark Reference", key="-DARK-", default=False)],
        ])],
        
        [sg.HorizontalSeparator()],
        
        # Control Buttons
        [sg.Button("Start Capture", key="-START-", size=(15, 1), button_color=("white", "green")),
         sg.Button("Stop", key="-STOP-", size=(15, 1), button_color=("white", "red"), disabled=True)],
        
        # Progress Section
        [sg.Frame("Progress", [
            [sg.Text("Status: Ready", key="-STATUS-", size=(50, 1))],
            [sg.Text("Progress:"), sg.ProgressBar(100, orientation='h', size=(40, 20), key="-PROGRESS-")],
            [sg.Text("Current Well: None", key="-CURRENT_WELL-", size=(50, 1))],
        ])],
        
        [sg.HorizontalSeparator()],
        [sg.Button("Exit", key="-EXIT-", size=(10, 1))],
    ]
    return layout


def gui_main():
    """Main GUI entry point."""
    if sg is None:
        print("ERROR: FreeSimpleGUI not available. Use command-line mode instead.")
        return
    
    sg.theme("LightGreen")
    window = sg.Window("Automated Blank Capture", create_gui_layout(), finalize=True)
    
    # Thread control
    capture_thread = None
    stop_flag = {"stop": False}
    running = False
    success_message = None
    error_message = None
    
    while True:
        event, values = window.read(timeout=100)  # 100ms timeout for responsiveness
        
        # Check for messages from capture thread and display popups in main thread
        if success_message:
            sg.popup(success_message, title="Success", keep_on_top=True)
            success_message = None
        if error_message:
            sg.popup_error(f"Error during capture:\n{error_message}", title="Error", keep_on_top=True)
            error_message = None
        
        if event == sg.WIN_CLOSED or event == "-EXIT-":
            if running:
                stop_flag["stop"] = True
                if capture_thread:
                    capture_thread.join(timeout=2)
            break
        
        elif event == "-START-" and not running:
            # Validate inputs
            try:
                num_rows = int(values["-ROWS-"])
                num_cols = int(values["-COLS-"])
                
                # Get corner positions
                top_left = {
                    "X": float(values["-TL_X-"]),
                    "Y": float(values["-TL_Y-"]),
                    "Z": float(values["-TL_Z-"])
                }
                bottom_left = {
                    "X": float(values["-BL_X-"]),
                    "Y": float(values["-BL_Y-"]),
                    "Z": float(values["-BL_Z-"])
                }
                top_right = {
                    "X": float(values["-TR_X-"]),
                    "Y": float(values["-TR_Y-"]),
                    "Z": float(values["-TR_Z-"])
                }
                bottom_right = {
                    "X": float(values["-BR_X-"]),
                    "Y": float(values["-BR_Y-"]),
                    "Z": float(values["-BR_Z-"])
                }
                
                output_file = values["-OUTPUT-"] or "well_blanks.json"
                settle_time = float(values["-SETTLE-"] or "1.0")
                use_dummy = values["-DUMMY-"]
                capture_dark = values["-DARK-"]
                
                # Check if config file is provided
                config_file = values["-CONFIG_FILE-"] if values["-CONFIG_FILE-"] else None
                
            except ValueError as e:
                sg.popup_error(f"Invalid input: {e}\n\nPlease check all numeric fields.", title="Input Error")
                continue
            
            # Capture dark reference if requested (before starting thread)
            dark_ref = None
            if capture_dark:
                window["-STATUS-"].update("Waiting for dark reference capture...")
                window.refresh()
                
                # Print to console for Pi users
                print("\n" + "="*60)
                print("DARK REFERENCE CAPTURE")
                print("="*60)
                print("Please cover the sensor, then click 'Capture' in the popup window.")
                print("Or close the popup to skip dark reference capture.")
                print("="*60 + "\n")
                
                # Create a custom modal popup window for dark reference
                # Use larger buttons and simpler layout for better Pi compatibility
                popup_layout = [
                    [sg.Text("DARK REFERENCE CAPTURE", font=("Helvetica", 12, "bold"), justification='center')],
                    [sg.Text("Please cover the sensor, then click 'Capture'", 
                            size=(45, 2), justification='center')],
                    [sg.Text("")],  # Spacer
                    [sg.Button("Capture", key="-CAPTURE-", size=(15, 2), button_color=("white", "green"), font=("Helvetica", 11, "bold"))],
                    [sg.Text("")],  # Spacer
                    [sg.Button("Skip", key="-SKIP-", size=(15, 2), button_color=("white", "orange"), font=("Helvetica", 11))],
                ]
                popup_window = sg.Window("Dark Reference", popup_layout, modal=True, keep_on_top=True, 
                                        finalize=True, location=(None, None), grab_anywhere=False)
                
                # Bring window to front (helps on Pi)
                try:
                    popup_window.bring_to_front()
                except:
                    pass
                
                # Blocking read for the popup window with timeout to prevent hanging
                popup_response = None
                timeout_count = 0
                max_timeout = 600  # 60 seconds max wait (600 * 100ms)
                
                while timeout_count < max_timeout:
                    popup_event, popup_values = popup_window.read(timeout=100)
                    if popup_event == sg.WIN_CLOSED or popup_event == "-SKIP-":
                        popup_response = "Skip"
                        print("Dark reference capture SKIPPED by user.")
                        break
                    elif popup_event == "-CAPTURE-":
                        popup_response = "Capture"
                        break
                    elif popup_event == sg.TIMEOUT_KEY:
                        timeout_count += 1
                        # Refresh window periodically to keep it responsive
                        if timeout_count % 10 == 0:
                            popup_window.refresh()
                        continue
                
                popup_window.close()
                
                if popup_response == "Capture":
                    window["-STATUS-"].update("Capturing dark reference...")
                    window.refresh()
                    print("Capturing dark reference...")
                    
                    # Initialize sensor temporarily for dark capture
                    from as7343_wellplate import init_sensor, read_channels, LABELS
                    temp_sensor = init_sensor()
                    dark_ref = read_channels(temp_sensor, averages=DEFAULT_AVG, settle_ms=0)
                    
                    # Print confirmation with values
                    print("\n" + "="*60)
                    print("DARK REFERENCE CAPTURED SUCCESSFULLY")
                    print("="*60)
                    print(f"Channel values:")
                    for i, (label, value) in enumerate(zip(LABELS, dark_ref)):
                        print(f"  {label}: {value:.2f}")
                    print("="*60)
                    print("Dark reference will be used for all well measurements.")
                    print("="*60 + "\n")
                    
                    window["-STATUS-"].update("Dark reference captured. Starting well capture...")
                    window.refresh()
                else:
                    # User skipped dark reference, but continue with capture
                    dark_ref = None
                    print("Continuing without dark reference...\n")
            
            # Disable start button, enable stop button
            window["-START-"].update(disabled=True)
            window["-STOP-"].update(disabled=False)
            running = True
            stop_flag["stop"] = False
            
            # Update status
            window["-STATUS-"].update("Initializing...")
            window["-PROGRESS-"].update(0, 100)
            window.refresh()
            
            # Run capture in separate thread
            def run_capture():
                nonlocal success_message, error_message, running
                try:
                    capture_blanks_automated(
                        config_file=config_file,
                        num_rows=num_rows,
                        num_cols=num_cols,
                        top_left=top_left if not config_file else None,
                        bottom_left=bottom_left if not config_file else None,
                        top_right=top_right if not config_file else None,
                        bottom_right=bottom_right if not config_file else None,
                        output_file=output_file,
                        settle_time=settle_time,
                        use_dummy_printer=use_dummy,
                        gui_window=window,
                        gui_progress_key="-PROGRESS-",
                        gui_status_key="-STATUS-",
                        gui_stop_flag=stop_flag,
                        dark_reference=dark_ref
                    )
                    window["-STATUS-"].update("Capture completed successfully!")
                    window["-CURRENT_WELL-"].update("All wells processed")
                    window["-PROGRESS-"].update(100)
                    # Store success message for main thread to display
                    success_message = f"Capture completed!\n\nSaved to: {output_file}"
                except Exception as e:
                    window["-STATUS-"].update(f"Error: {str(e)}")
                    # Store error message for main thread to display
                    error_message = str(e)
                finally:
                    # Re-enable start button, disable stop button
                    window["-START-"].update(disabled=False)
                    window["-STOP-"].update(disabled=True)
                    running = False
            
            capture_thread = threading.Thread(target=run_capture, daemon=True)
            capture_thread.start()
        
        elif event == "-STOP-" and running:
            stop_flag["stop"] = True
            window["-STATUS-"].update("Stopping capture...")
            window.refresh()
    
    window.close()


def main():
    """Main entry point - supports both GUI and command-line interface."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Automated blank capture for well plates using AS7343 sensor"
    )
    parser.add_argument("--gui", action="store_true",
                       help="Launch GUI interface (default if no arguments provided)")
    parser.add_argument("--config", type=str, help="JSON file with well plate configuration")
    parser.add_argument("--output", type=str, default="well_blanks.json",
                       help="Output JSON file for blanks (default: well_blanks.json)")
    parser.add_argument("--settle", type=float, default=1.0,
                       help="Settle time after movement in seconds (default: 1.0)")
    parser.add_argument("--dummy-printer", action="store_true",
                       help="Use dummy printer mode (skip actual movement)")
    
    # Manual configuration options
    parser.add_argument("--rows", type=int, default=None,
                       help=f"Number of rows (default: {DEFAULT_NUM_ROWS} from code)")
    parser.add_argument("--cols", type=int, default=None,
                       help=f"Number of columns (default: {DEFAULT_NUM_COLS} from code)")
    parser.add_argument("--top-left", type=str, help="Top-left position as 'X,Y,Z'")
    parser.add_argument("--bottom-left", type=str, help="Bottom-left position as 'X,Y,Z'")
    parser.add_argument("--top-right", type=str, help="Top-right position as 'X,Y,Z'")
    parser.add_argument("--bottom-right", type=str, help="Bottom-right position as 'X,Y,Z'")
    
    args = parser.parse_args()
    
    # If no arguments provided or --gui flag, launch GUI
    if args.gui or (len(sys.argv) == 1):
        if sg is None:
            print("ERROR: FreeSimpleGUI not available. Install with: pip install FreeSimpleGUI")
            print("Falling back to command-line mode...")
        else:
            gui_main()
            return
    
    # Otherwise, use command-line mode
    # Parse manual positions if provided
    top_left = bottom_left = top_right = bottom_right = None
    if args.top_left:
        x, y, z = map(float, args.top_left.split(','))
        top_left = {"X": x, "Y": y, "Z": z}
    if args.bottom_left:
        x, y, z = map(float, args.bottom_left.split(','))
        bottom_left = {"X": x, "Y": y, "Z": z}
    if args.top_right:
        x, y, z = map(float, args.top_right.split(','))
        top_right = {"X": x, "Y": y, "Z": z}
    if args.bottom_right:
        x, y, z = map(float, args.bottom_right.split(','))
        bottom_right = {"X": x, "Y": y, "Z": z}
    
    # Use defaults from global variables if not provided
    num_rows = args.rows if args.rows is not None else DEFAULT_NUM_ROWS
    num_cols = args.cols if args.cols is not None else DEFAULT_NUM_COLS
    
    # Use defaults for corners if not provided
    if not top_left:
        top_left = DEFAULT_TOP_LEFT
    if not bottom_left:
        bottom_left = DEFAULT_BOTTOM_LEFT
    if not top_right:
        top_right = DEFAULT_TOP_RIGHT
    if not bottom_right:
        bottom_right = DEFAULT_BOTTOM_RIGHT
    
    # Run automation
    capture_blanks_automated(
        config_file=args.config,
        num_rows=num_rows,
        num_cols=num_cols,
        top_left=top_left,
        bottom_left=bottom_left,
        top_right=top_right,
        bottom_right=bottom_right,
        output_file=args.output,
        settle_time=args.settle,
        use_dummy_printer=args.dummy_printer
    )


if __name__ == "__main__":
    main()

