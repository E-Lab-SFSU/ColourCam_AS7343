import PySimpleGUI as sg
import serial
import serial.tools.list_ports
import time
import sys

# Function to send G-code to the printer
# def send_gcode(command):
#     ser.write(command.encode('utf-8'))
#     ser.write(b'\n')
#     time.sleep(0.1)

# Updated send_gcode command waits until printer completes current action before proceeding
def send_gcode(command):
    ser.write((command + '\n').encode('utf-8'))  # Use '\r\n' if needed
    time.sleep(0.1)  # Initial delay for command processing
    while True:
        if ser.in_waiting > 0:  # Check if there's data waiting to be read
            response = ser.readline().decode('utf-8').strip()
            if "ok" in response:  # Assuming 'ok' is the acknowledgment from the printer
                break
            elif "error" in response:  # Handle potential error messages
                sg.popup(f"Error from printer: {response}", title='Printer Error', keep_on_top=True)
                break


# Initial positions
INITIAL_X, INITIAL_Y, INITIAL_Z = 200, 150, 170

# Global variables with initial, minimum, and maximum values
X, Y, Z = INITIAL_X, INITIAL_Y, INITIAL_Z
MAX_X, MAX_Y, MAX_Z = 200, 150, 170
MIN_X, MIN_Y, MIN_Z = 0, 0, 0

# Time of the last click and click interval in seconds
last_click_time = 0
click_interval = 0.5

# In case the port needs to be manually selected
# def find_serial_port():
#     ports = serial.tools.list_ports.comports()
#     usb_ports = [port for port in ports if 'USB' in port.description]
#     if not usb_ports:
#         print("No USB serial ports found.")
#         return None
#     print("Available USB serial ports:")
#     for i, port in enumerate(usb_ports):
#         print(f"{i}: {port.device} - {port.description}")
#     index = int(input("Enter the number of the port to use: "))
#     return usb_ports[index].device

def find_serial_port():
    ports = serial.tools.list_ports.comports()
    usb_ports = [port for port in ports if 'USB' in port.description]
    if not usb_ports:
        print("No USB serial ports found.")
        return None

    for usb_port in usb_ports:
        try:
            ser = serial.Serial(usb_port.device, 115200, timeout=1)
            ser.close()  # Close the port now that we know it works
            print(f"Selected port: {usb_port.device} - {usb_port.description}")
            return usb_port.device
        except serial.SerialException:
            print(f"Failed to connect on {usb_port.device}")

    print("No available ports responded.")
    return None

def wait_for_connection(serial_port):
    """Attempt to open a serial connection and wait until it is established."""
    baud_rate = 115200
    while True:
        try:
            ser = serial.Serial(serial_port, baud_rate, timeout=1)
            print(f"Connected to {serial_port} at {baud_rate} baud.")
            return ser
        except serial.SerialException:
            print(f"Waiting for connection on {serial_port}...")
            time.sleep(2)  # Wait a bit before trying to connect again

# Function to update the global variables with limit checks, rounding, and action execution
def update_axis(axis, increment):
    global X, Y, Z

    new_value = round((X if axis == 'X' else Y if axis == 'Y' else Z) + increment, 2)
    if MIN_X <= new_value <= MAX_X and axis == 'X' or \
       MIN_Y <= new_value <= MAX_Y and axis == 'Y' or \
       MIN_Z <= new_value <= MAX_Z and axis == 'Z':
        if axis == 'X':
            X = new_value
        elif axis == 'Y':
            Y = new_value
        elif axis == 'Z':
            Z = new_value
        send_gcode(f"G1 {axis}{new_value}")
    else:
        print(f"{axis} axis limit reached.")

    print(f"Updated values: X-{X}, Y-{Y}, Z-{Z}")

def home_printer():
    global X, Y, Z
    print('Homing Printer, please do wait for countdown to complete') # Countdown not really necessary, the program will wait until printer is done homing now
    send_gcode('G28')  # Homing command
    X, Y, Z = INITIAL_X, INITIAL_Y, INITIAL_Z
    for remaining in range(10, 0, -1):
        sys.stdout.write("\r")
        sys.stdout.write("{:2d} seconds remaining.".format(remaining))
        sys.stdout.flush()
        time.sleep(1)

    sys.stdout.write("\rComplete!            \n")
    print(f"Printer homed. Reset positions to X: {X}, Y: {Y}, Z: {Z}")
    
# Layout for the Radio Buttons and action buttons
layout = [
    [sg.Radio('X Axis', 'AXIS_CHOICE', key='X', default=True),
     sg.Radio('Y Axis', 'AXIS_CHOICE', key='Y'),
     sg.Radio('Z Axis', 'AXIS_CHOICE', key='Z')],
    
    [sg.Button('+ 0.1 mm', key='+0.1'), sg.Button('+ 1 mm', key='+1'), sg.Button('+ 10 mm', key='+10')],
    [sg.Button('- 0.1 mm', key='-0.1'), sg.Button('- 1 mm', key='-1'), sg.Button('- 10 mm', key='-10')],
    [sg.Button('Home Printer', key='-HOME-')]
]

# Create the window
window = sg.Window('XYZ Control Panel', layout)

# sleep and home
# Initialize Serial Connection
serial_port = find_serial_port()
ser = None
if serial_port:
    ser = wait_for_connection(serial_port)
home_printer()

# Event loop

while True:
    event, values = window.read()

    if event == sg.WIN_CLOSED:
        break

    if event in ('+0.1', '+1', '+10', '-0.1', '-1', '-10'):
        current_time = time.time()
        if current_time - last_click_time < click_interval:
            sg.popup('\n\n\n\n\nClicked too fast!\n\n\n\n\n', title='Warning')
        else:
            selected_axis = 'X' if values['X'] else 'Y' if values['Y'] else 'Z'
            increment = float(event.replace(' mm', '').replace('+', ''))
            update_axis(selected_axis, increment)
            last_click_time = current_time

    elif event == '-HOME-':
        home_printer()

# Close the window and serial connection
window.close()
ser.close()
