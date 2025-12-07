#!/usr/bin/env python3
"""
Well Plate Location Calculator with Printer Control
Integrates printer movement, corner setting, and snake path generation.

Features:
- Connect to 3D printer via serial
- Home printer
- Move in X, Y, Z axes
- Get current position and set corners
- Calculate all well positions
- Generate snake path
- Save configuration

Author: Integrated from existing modules
Date: 2025
"""

import FreeSimpleGUI as sg
import serial
import serial.tools.list_ports
import time
import json
from datetime import datetime

# ===== Printer Control Functions (from XYZGUI.py) =====

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
                # Read raw bytes and decode with error handling
                raw_data = ser.readline()
                # Try UTF-8 first, fallback to latin-1 (which can decode any byte)
                try:
                    response = raw_data.decode('utf-8').strip()
                except UnicodeDecodeError:
                    # Fallback to latin-1 which can decode any byte sequence
                    response = raw_data.decode('latin-1', errors='ignore').strip()
                
                # Check for acknowledgment or error
                response_lower = response.lower()
                if "ok" in response_lower:
                    break
                elif "error" in response_lower:
                    return False  # Error occurred
            except Exception:
                # If decoding completely fails, just continue waiting
                time.sleep(0.01)
                continue
    return True

def find_serial_port():
    """Find and return the first available USB serial port."""
    ports = serial.tools.list_ports.comports()
    usb_ports = []
    for port in ports:
        device = port.device
        description = port.description.upper() if port.description else ""
        
        # Check if it's a USB serial port
        is_usb_serial = (
            'ttyUSB' in device or 
            'ttyACM' in device or 
            'USB' in description or
            'Serial' in description or
            'CH340' in description or
            'FTDI' in description or
            'CP210' in description
        )
        
        exclude = (
            'BLUETOOTH' in description or
            'ttyAMA0' in device or
            'ttyS0' in device
        )
        
        if is_usb_serial and not exclude:
            usb_ports.append(port)
    
    if not usb_ports:
        return None
    
    # Try each port
    for usb_port in usb_ports:
        try:
            ser = serial.Serial(usb_port.device, 115200, timeout=1)
            ser.close()
            return usb_port.device
        except serial.SerialException:
            continue
    
    return None

def wait_for_connection(serial_port):
    """Attempt to open a serial connection and wait until it is established."""
    baud_rate = 115200
    max_attempts = 10
    attempt = 0
    
    while attempt < max_attempts:
        try:
            ser = serial.Serial(serial_port, baud_rate, timeout=1)
            return ser
        except serial.SerialException:
            attempt += 1
            if attempt >= max_attempts:
                return None
            time.sleep(2)
    return None

def get_current_position(ser):
    """
    Get current printer position by sending M114 (Get Current Position).
    Returns (X, Y, Z) or None if failed.
    """
    try:
        ser.write(b'M114\n')
        time.sleep(0.2)
        
        # Read response
        position = {"X": None, "Y": None, "Z": None}
        timeout = time.time() + 2.0  # 2 second timeout
        
        while time.time() < timeout:
            if ser.in_waiting > 0:
                try:
                    raw_data = ser.readline()
                    try:
                        response = raw_data.decode('utf-8').strip()
                    except UnicodeDecodeError:
                        response = raw_data.decode('latin-1', errors='ignore').strip()
                    
                    # Parse M114 response: "X:100.00 Y:200.00 Z:50.00 E:0.00 Count X:0 Y:0 Z:0"
                    if "X:" in response:
                        parts = response.split()
                        for part in parts:
                            if part.startswith("X:"):
                                position["X"] = float(part[2:])
                            elif part.startswith("Y:"):
                                position["Y"] = float(part[2:])
                            elif part.startswith("Z:"):
                                position["Z"] = float(part[2:])
                        
                        if all(v is not None for v in position.values()):
                            return position
                except Exception:
                    continue
            time.sleep(0.01)
        
        return None
    except Exception:
        return None

# ===== Well Position Calculation (from auto_blank_capture.py) =====

def calculate_well_positions(top_left, bottom_left, top_right, bottom_right, num_rows, num_cols):
    """
    Calculate all well positions using bilinear interpolation from 4 corners.
    """
    well_positions = {}
    
    def row_to_index(row_letter):
        return ord(row_letter.upper()) - ord('A')
    
    # Generate well names
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
        
        row_idx = row_to_index(row_letter)
        col_idx = col_num - 1
        
        # Normalized coordinates (0 to 1)
        u = col_idx / (num_cols - 1) if num_cols > 1 else 0.0
        v = row_idx / (num_rows - 1) if num_rows > 1 else 0.0
        
        # Bilinear interpolation
        top_x = tl_x + u * (tr_x - tl_x)
        top_y = tl_y + u * (tr_y - tl_y)
        top_z = tl_z + u * (tr_z - tl_z)
        
        bottom_x = bl_x + u * (br_x - bl_x)
        bottom_y = bl_y + u * (br_y - bl_y)
        bottom_z = bl_z + u * (br_z - bl_z)
        
        x = top_x + v * (bottom_x - top_x)
        y = top_y + v * (bottom_y - top_y)
        z = top_z + v * (bottom_z - top_z)
        
        well_positions[well] = {"X": round(x, 2), "Y": round(y, 2), "Z": round(z, 2)}
    
    return well_positions

def generate_snake_path(num_rows, num_cols):
    """
    Generate snake path through wells.
    Pattern: A1->A2->A3->A4, then B4->B3->B2->B1, then C1->C2->C3->C4, etc.
    """
    path = []
    for r in range(num_rows):
        row_letter = chr(ord('A') + r)
        if r % 2 == 0:  # Even rows: left to right
            for c in range(1, num_cols + 1):
                path.append(f"{row_letter}{c}")
        else:  # Odd rows: right to left
            for c in range(num_cols, 0, -1):
                path.append(f"{row_letter}{c}")
    return path

# ===== Main GUI =====

def main():
    sg.theme("LightGreen")
    
    # State variables
    ser = None
    current_x, current_y, current_z = 0.0, 0.0, 0.0
    num_rows = 3
    num_cols = 4
    
    # Corner positions
    corners = {
        "top_left": {"X": 0.0, "Y": 0.0, "Z": 0.0},
        "bottom_left": {"X": 0.0, "Y": 0.0, "Z": 0.0},
        "top_right": {"X": 0.0, "Y": 0.0, "Z": 0.0},
        "bottom_right": {"X": 0.0, "Y": 0.0, "Z": 0.0}
    }
    corners_set = {
        "top_left": False,
        "bottom_left": False,
        "top_right": False,
        "bottom_right": False
    }
    
    # Layout
    layout = [
        [sg.Text("Well Plate Location Calculator with Printer Control", font=("Helvetica", 16, "bold"))],
        [sg.HorizontalSeparator()],
        
        # Printer Connection Section
        [sg.Frame("Printer Connection", [
            [sg.Text("Status:", key="-PRINTER_STATUS-", size=(20, 1)),
             sg.Button("Connect", key="-CONNECT-"),
             sg.Button("Home", key="-HOME-", disabled=True)],
            [sg.Text("Current Position: X=0.00, Y=0.00, Z=0.00", key="-CURRENT_POS-", size=(50, 1)),
             sg.Button("Get Position", key="-GET_POS-", disabled=True)],
        ])],
        
        # Movement Controls
        [sg.Frame("Printer Movement", [
            [sg.Radio('X Axis', 'AXIS', key='-AXIS_X-', default=True),
             sg.Radio('Y Axis', 'AXIS', key='-AXIS_Y-'),
             sg.Radio('Z Axis', 'AXIS', key='-AXIS_Z-')],
            [sg.Button('+ 0.1 mm', key='+0.1', disabled=True),
             sg.Button('+ 1 mm', key='+1', disabled=True),
             sg.Button('+ 10 mm', key='+10', disabled=True)],
            [sg.Button('- 0.1 mm', key='-0.1', disabled=True),
             sg.Button('- 1 mm', key='-1', disabled=True),
             sg.Button('- 10 mm', key='-10', disabled=True)],
        ])],
        
        [sg.HorizontalSeparator()],
        
        # Well Plate Configuration
        [sg.Frame("Well Plate Configuration", [
            [sg.Text("Rows:"), sg.Input("3", size=(5, 1), key="-ROWS-"),
             sg.Text("Columns:"), sg.Input("4", size=(5, 1), key="-COLS-")],
        ])],
        
        # Corner Setting
        [sg.Frame("Set Corners", [
            [sg.Text("Top-Left:"), 
             sg.Input("0.00", size=(8, 1), key="-TL_X-"),
             sg.Input("0.00", size=(8, 1), key="-TL_Y-"),
             sg.Input("0.00", size=(8, 1), key="-TL_Z-"),
             sg.Button("Set from Printer", key="-SET_TL-", disabled=True),
             sg.Button("Set from Input", key="-SET_TL_MANUAL-"),
             sg.Text("", key="-TL_STATUS-", size=(10, 1))],
            [sg.Text("Bottom-Left:"),
             sg.Input("0.00", size=(8, 1), key="-BL_X-"),
             sg.Input("0.00", size=(8, 1), key="-BL_Y-"),
             sg.Input("0.00", size=(8, 1), key="-BL_Z-"),
             sg.Button("Set from Printer", key="-SET_BL-", disabled=True),
             sg.Button("Set from Input", key="-SET_BL_MANUAL-"),
             sg.Text("", key="-BL_STATUS-", size=(10, 1))],
            [sg.Text("Top-Right:"),
             sg.Input("0.00", size=(8, 1), key="-TR_X-"),
             sg.Input("0.00", size=(8, 1), key="-TR_Y-"),
             sg.Input("0.00", size=(8, 1), key="-TR_Z-"),
             sg.Button("Set from Printer", key="-SET_TR-", disabled=True),
             sg.Button("Set from Input", key="-SET_TR_MANUAL-"),
             sg.Text("", key="-TR_STATUS-", size=(10, 1))],
            [sg.Text("Bottom-Right:"),
             sg.Input("0.00", size=(8, 1), key="-BR_X-"),
             sg.Input("0.00", size=(8, 1), key="-BR_Y-"),
             sg.Input("0.00", size=(8, 1), key="-BR_Z-"),
             sg.Button("Set from Printer", key="-SET_BR-", disabled=True),
             sg.Button("Set from Input", key="-SET_BR_MANUAL-"),
             sg.Text("", key="-BR_STATUS-", size=(10, 1))],
        ])],
        
        [sg.HorizontalSeparator()],
        
        # Generate and Save
        [sg.Frame("Generate Path", [
            [sg.Button("Calculate Well Positions", key="-CALC_POS-", disabled=True),
             sg.Button("Generate Snake Path", key="-GEN_SNAKE-", disabled=True)],
            [sg.Text("Output File:"), sg.Input("well_config.json", size=(30, 1), key="-OUTPUT_FILE-"),
             sg.Button("Save Configuration", key="-SAVE-", disabled=True)],
        ])],
        
        # Results Display
        [sg.Frame("Results", [
            [sg.Multiline("", size=(70, 10), key="-RESULTS-", autoscroll=True, disabled=True)],
        ])],
        
        [sg.HorizontalSeparator()],
        [sg.Button("Exit", key="-EXIT-")],
    ]
    
    window = sg.Window("Well Plate Location Calculator", layout, finalize=True)
    
    # Helper function to set corner and check if all are set
    def set_corner(corner_name, x, y, z, status_key):
        nonlocal corners, corners_set
        corners[corner_name] = {"X": x, "Y": y, "Z": z}
        corners_set[corner_name] = True
        window[status_key].update("✓ Set")
        
        # Debug: print corner status
        print(f"Set {corner_name}: {corners_set}")
        
        # Enable calculate button if all corners are set
        all_set = all(corners_set.values())
        print(f"All corners set? {all_set}, Values: {list(corners_set.values())}")
        
        if all_set:
            try:
                # Enable all three buttons at once
                window["-CALC_POS-"].update(disabled=False)
                window["-GEN_SNAKE-"].update(disabled=False)
                window["-SAVE-"].update(disabled=False)
                window.refresh()  # Force GUI update
                print("All buttons enabled!")
            except Exception as e:
                print(f"Error enabling button: {e}")
                import traceback
                traceback.print_exc()
    
    # Event loop
    while True:
        event, values = window.read(timeout=100)
        
        if event == sg.WIN_CLOSED or event == "-EXIT-":
            break
        
        # Connect to printer
        elif event == "-CONNECT-":
            window["-PRINTER_STATUS-"].update("Connecting...")
            window.refresh()
            
            serial_port = find_serial_port()
            if serial_port:
                ser = wait_for_connection(serial_port)
                if ser:
                    window["-PRINTER_STATUS-"].update(f"Connected: {serial_port}")
                    window["-HOME-"].update(disabled=False)
                    window["-GET_POS-"].update(disabled=False)
                    # Enable movement buttons
                    for key in ['+0.1', '+1', '+10', '-0.1', '-1', '-10']:
                        window[key].update(disabled=False)
                    # Enable corner setting buttons
                    for key in ["-SET_TL-", "-SET_BL-", "-SET_TR-", "-SET_BR-"]:
                        window[key].update(disabled=False)
                    window["-CONNECT-"].update(disabled=True)
                else:
                    window["-PRINTER_STATUS-"].update("Connection failed")
                    sg.popup_error("Failed to connect to printer", title="Error")
            else:
                window["-PRINTER_STATUS-"].update("No port found")
                sg.popup_error("No USB serial port found", title="Error")
        
        # Home printer
        elif event == "-HOME-" and ser:
            window["-PRINTER_STATUS-"].update("Homing...")
            window.refresh()
            if send_gcode(ser, "G28"):
                window["-PRINTER_STATUS-"].update("Homed")
                # Get position after homing
                pos = get_current_position(ser)
                if pos:
                    current_x, current_y, current_z = pos["X"], pos["Y"], pos["Z"]
                    window["-CURRENT_POS-"].update(f"Current Position: X={current_x:.2f}, Y={current_y:.2f}, Z={current_z:.2f}")
            else:
                window["-PRINTER_STATUS-"].update("Homing failed")
        
        # Get current position
        elif event == "-GET_POS-" and ser:
            pos = get_current_position(ser)
            if pos:
                current_x, current_y, current_z = pos["X"], pos["Y"], pos["Z"]
                window["-CURRENT_POS-"].update(f"Current Position: X={current_x:.2f}, Y={current_y:.2f}, Z={current_z:.2f}")
            else:
                sg.popup("Failed to get position. Make sure printer is responsive.", title="Warning")
        
        # Movement buttons
        elif event in ('+0.1', '+1', '+10', '-0.1', '-1', '-10') and ser:
            # Determine axis
            axis = 'X' if values['-AXIS_X-'] else 'Y' if values['-AXIS_Y-'] else 'Z'
            
            # Determine increment
            increment = float(event.replace(' mm', '').replace('+', ''))
            
            # Get current position
            current = current_x if axis == 'X' else current_y if axis == 'Y' else current_z
            new_value = round(current + increment, 2)
            
            # Move
            command = f"G1 {axis}{new_value} F3000"
            if send_gcode(ser, command):
                # Update current position
                if axis == 'X':
                    current_x = new_value
                elif axis == 'Y':
                    current_y = new_value
                else:
                    current_z = new_value
                
                window["-CURRENT_POS-"].update(f"Current Position: X={current_x:.2f}, Y={current_y:.2f}, Z={current_z:.2f}")
        
        # Set corners from printer position
        elif event == "-SET_TL-" and ser:
            set_corner("top_left", current_x, current_y, current_z, "-TL_STATUS-")
            window["-TL_X-"].update(f"{current_x:.2f}")
            window["-TL_Y-"].update(f"{current_y:.2f}")
            window["-TL_Z-"].update(f"{current_z:.2f}")
        
        elif event == "-SET_BL-" and ser:
            set_corner("bottom_left", current_x, current_y, current_z, "-BL_STATUS-")
            window["-BL_X-"].update(f"{current_x:.2f}")
            window["-BL_Y-"].update(f"{current_y:.2f}")
            window["-BL_Z-"].update(f"{current_z:.2f}")
        
        elif event == "-SET_TR-" and ser:
            set_corner("top_right", current_x, current_y, current_z, "-TR_STATUS-")
            window["-TR_X-"].update(f"{current_x:.2f}")
            window["-TR_Y-"].update(f"{current_y:.2f}")
            window["-TR_Z-"].update(f"{current_z:.2f}")
        
        elif event == "-SET_BR-" and ser:
            set_corner("bottom_right", current_x, current_y, current_z, "-BR_STATUS-")
            window["-BR_X-"].update(f"{current_x:.2f}")
            window["-BR_Y-"].update(f"{current_y:.2f}")
            window["-BR_Z-"].update(f"{current_z:.2f}")
        
        # Set corners from manual input
        elif event == "-SET_TL_MANUAL-":
            try:
                x = float(values["-TL_X-"])
                y = float(values["-TL_Y-"])
                z = float(values["-TL_Z-"])
                set_corner("top_left", x, y, z, "-TL_STATUS-")
            except ValueError:
                sg.popup_error("Please enter valid numbers for X, Y, Z", title="Error")
        
        elif event == "-SET_BL_MANUAL-":
            try:
                x = float(values["-BL_X-"])
                y = float(values["-BL_Y-"])
                z = float(values["-BL_Z-"])
                set_corner("bottom_left", x, y, z, "-BL_STATUS-")
            except ValueError:
                sg.popup_error("Please enter valid numbers for X, Y, Z", title="Error")
        
        elif event == "-SET_TR_MANUAL-":
            try:
                x = float(values["-TR_X-"])
                y = float(values["-TR_Y-"])
                z = float(values["-TR_Z-"])
                set_corner("top_right", x, y, z, "-TR_STATUS-")
            except ValueError:
                sg.popup_error("Please enter valid numbers for X, Y, Z", title="Error")
        
        elif event == "-SET_BR_MANUAL-":
            try:
                x = float(values["-BR_X-"])
                y = float(values["-BR_Y-"])
                z = float(values["-BR_Z-"])
                set_corner("bottom_right", x, y, z, "-BR_STATUS-")
            except ValueError:
                sg.popup_error("Please enter valid numbers for X, Y, Z", title="Error")
        
        # Calculate well positions
        elif event == "-CALC_POS-":
            # Double-check corners are set
            if not all(corners_set.values()):
                missing = [k.replace("_", " ").title() for k, v in corners_set.items() if not v]
                sg.popup_error(f"Please set all 4 corners first.\nMissing: {', '.join(missing)}", title="Error")
                continue
            
            try:
                num_rows = int(values["-ROWS-"])
                num_cols = int(values["-COLS-"])
            except ValueError:
                sg.popup_error("Invalid rows/columns. Please enter numbers.", title="Error")
                continue
            
            try:
                well_positions = calculate_well_positions(
                    corners["top_left"],
                    corners["bottom_left"],
                    corners["top_right"],
                    corners["bottom_right"],
                    num_rows,
                    num_cols
                )
                
                # Display results
                result_text = f"Calculated {len(well_positions)} well positions:\n\n"
                for well, pos in well_positions.items():
                    result_text += f"{well}: X={pos['X']:.2f}, Y={pos['Y']:.2f}, Z={pos['Z']:.2f}\n"
                
                window["-RESULTS-"].update(result_text)
                window["-GEN_SNAKE-"].update(disabled=False)
                window["-SAVE-"].update(disabled=False)
                sg.popup(f"Successfully calculated {len(well_positions)} well positions!", title="Success")
            except Exception as e:
                sg.popup_error(f"Error calculating positions:\n{str(e)}", title="Error")
        
        # Generate snake path
        elif event == "-GEN_SNAKE-":
            try:
                num_rows = int(values["-ROWS-"])
                num_cols = int(values["-COLS-"])
            except ValueError:
                sg.popup_error("Invalid rows/columns.", title="Error")
                continue
            
            snake_path = generate_snake_path(num_rows, num_cols)
            
            # Get well positions if not already calculated
            if not all(corners_set.values()):
                sg.popup_error("Please set all corners and calculate positions first.", title="Error")
                continue
            
            well_positions = calculate_well_positions(
                corners["top_left"],
                corners["bottom_left"],
                corners["top_right"],
                corners["bottom_right"],
                num_rows,
                num_cols
            )
            
            # Display snake path with positions
            result_text = window["-RESULTS-"].get() + "\n\nSnake Path:\n"
            for i, well in enumerate(snake_path, 1):
                pos = well_positions[well]
                result_text += f"{i}. {well}: X={pos['X']:.2f}, Y={pos['Y']:.2f}, Z={pos['Z']:.2f}\n"
            
            window["-RESULTS-"].update(result_text)
        
        # Save configuration
        elif event == "-SAVE-":
            try:
                num_rows = int(values["-ROWS-"])
                num_cols = int(values["-COLS-"])
            except ValueError:
                sg.popup_error("Invalid rows/columns.", title="Error")
                continue
            
            if not all(corners_set.values()):
                sg.popup_error("Please set all corners first.", title="Error")
                continue
            
            try:
                well_positions = calculate_well_positions(
                    corners["top_left"],
                    corners["bottom_left"],
                    corners["top_right"],
                    corners["bottom_right"],
                    num_rows,
                    num_cols
                )
                
                snake_path = generate_snake_path(num_rows, num_cols)
                
                output_file = values["-OUTPUT_FILE-"] or "well_config.json"
                
                config = {
                    "timestamp": datetime.now().isoformat(),
                    "num_rows": num_rows,
                    "num_cols": num_cols,
                    "top_left": corners["top_left"],
                    "bottom_left": corners["bottom_left"],
                    "top_right": corners["top_right"],
                    "bottom_right": corners["bottom_right"],
                    "well_positions": well_positions,
                    "snake_path": snake_path
                }
                
                with open(output_file, 'w') as f:
                    json.dump(config, f, indent=2)
                sg.popup(f"Configuration saved to:\n{output_file}", title="Success")
            except Exception as e:
                sg.popup_error(f"Failed to save:\n{str(e)}", title="Error")
    
    # Cleanup
    if ser:
        ser.close()
    window.close()

if __name__ == "__main__":
    main()

