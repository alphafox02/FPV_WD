#!/usr/bin/env python3
"""
fpv_detector.py
cemaxecuter 2025

Connects to a specified serial port, receives JSON-like messages from an FPV detection sensor,
enriches them with GPS data from gpsd, and publishes via a ZMQ XPUB socket for better scalability.

Usage:
    python3 fpv_detector.py --serial /dev/ttyACM0 --baud 115200 --zmq-port 4020 --stationary --debug

Options:
    --serial        Path to the serial device (default: /dev/ACM0)
    --baud          Baud rate for serial communication (default: 115200)
    --zmq-port      ZMQ port to publish messages on (default: 4020)
    --stationary    If true, read GPS location only once at startup
                    (assumes the WarDragon hardware is stationary).
    --debug         Enable debug output to console.
"""

import json
import logging
import time
import argparse
import serial
import zmq
from gps3 import gps3

# Defaults
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_BAUD_RATE = 115200
DEFAULT_ZMQ_PORT = 4020
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947
RECONNECT_DELAY = 5  # Seconds

def parse_args():
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description="FPV Detector: Reads JSON from serial, adds GPS, publishes via ZMQ."
    )
    parser.add_argument("--serial", default=DEFAULT_SERIAL_PORT,
                        help="Serial port to connect to.")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD_RATE,
                        help="Baud rate for serial communication.")
    parser.add_argument("--zmq-port", type=int, default=DEFAULT_ZMQ_PORT,
                        help="ZMQ port to publish messages on.")
    parser.add_argument("--stationary", action="store_true",
                        help="If set, read GPS once at startup (assumes WarDragon is stationary).")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging.")
    return parser.parse_args()

def setup_logging(debug: bool):
    """Configures logging."""
    # Use WARNING level if not in debug mode for minimal logging
    log_level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

def init_gps_connection():
    """Initialize a persistent gpsd connection (socket + data_stream)."""
    gps_socket = gps3.GPSDSocket()
    data_stream = gps3.DataStream()
    gps_socket.connect(host=GPSD_HOST, port=GPSD_PORT)
    gps_socket.watch()
    return gps_socket, data_stream

def get_gps_location(gps_socket, data_stream):
    """Fetches GPS coordinates from an already-connected gpsd socket (non-blocking)."""
    try:
        new_data = gps_socket.next()
        if new_data:
            data_stream.unpack(new_data)
            lat = data_stream.TPV.get('lat', 0.0)
            lon = data_stream.TPV.get('lon', 0.0)
            return lat, lon
    except StopIteration:
        # No data right now
        pass
    return 0.0, 0.0

def read_serial(serial_port, baud_rate):
    """Attempts to connect and read from the serial port continuously."""
    while True:
        try:
            with serial.Serial(serial_port, baud_rate, timeout=1) as ser:
                logging.warning(f"Connected to {serial_port} at {baud_rate} baud.")
                while True:
                    line = ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        logging.debug(f"Raw data: {line}")
                        yield line
        except serial.SerialException as e:
            logging.error(f"Serial connection error: {e}. Reconnecting in {RECONNECT_DELAY} seconds...")
            time.sleep(RECONNECT_DELAY)

def main():
    args = parse_args()
    setup_logging(args.debug)

    # Initialize GPS if not stationary, or read once if stationary
    gps_socket, data_stream = None, None
    lat_cache, lon_cache = 0.0, 0.0

    # If stationary, fetch GPS coordinates one time only
    # Otherwise, keep a persistent connection for continuous updates.
    if not args.stationary:
        gps_socket, data_stream = init_gps_connection()
    else:
        gps_socket, data_stream = init_gps_connection()
        lat_cache, lon_cache = get_gps_location(gps_socket, data_stream)
        gps_socket.close()
        gps_socket = None
        data_stream = None

    # Setup ZMQ XPUB socket for multiple subscribers
    context = zmq.Context()
    zmq_socket = context.socket(zmq.XPUB)
    zmq_socket.setsockopt(zmq.SNDHWM, 1000)  # High-water mark for buffering
    zmq_socket.setsockopt(zmq.LINGER, 0)     # Ensure clean socket shutdown
    zmq_socket.bind(f"tcp://0.0.0.0:{args.zmq_port}")
    logging.warning(f"ZMQ XPUB socket bound to tcp://0.0.0.0:{args.zmq_port}")

    try:
        for line in read_serial(args.serial, args.baud):
            try:
                # Parse incoming JSON-like data
                data = json.loads(line)

                msg_type = data.get("type", "")
                if msg_type == "nodeMsg":
                    logging.warning("Boot message received.")
                elif msg_type == "nodeAlert":
                    # Safely fetch the 'stat' field in msg
                    stat = data.get("msg", {}).get("stat", "")
                    if "NEW CONTACT LOCK" in stat:
                        logging.warning("New FPV Drone detected!")
                    elif "LOCK UPDATE" in stat:
                        logging.warning("FPV Drone still in view.")
                    elif "LOST CONTACT LOCK" in stat:
                        logging.warning("FPV Drone signal lost.")
                # -----------------------------------------------------

                # Update GPS location if WarDragon is mobile
                if not args.stationary and gps_socket and data_stream:
                    current_lat, current_lon = get_gps_location(gps_socket, data_stream)
                    lat, lon = (current_lat, current_lon)
                else:
                    # If stationary, use cached location
                    lat, lon = (lat_cache, lon_cache)

                data["gps_lat"] = lat
                data["gps_lon"] = lon

                # Publish the data
                json_message = json.dumps(data)
                zmq_socket.send_string(json_message)
                logging.debug(f"Published: {json_message}")

            except json.JSONDecodeError:
                logging.warning(f"Failed to parse JSON: {line}")
            except Exception as e:
                logging.error(f"Unexpected error: {e}")

    except KeyboardInterrupt:
        logging.warning("Shutting down ZMQ server...")
    finally:
        zmq_socket.close()
        context.term()
        if gps_socket:
            gps_socket.close()

if __name__ == "__main__":
    main()
