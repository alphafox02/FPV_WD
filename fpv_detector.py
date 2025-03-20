#!/usr/bin/env python3
"""
MIT License

Copyright (c) 2025 Cemaxecuter

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

"""
fpv_detector.py

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
import sys
import signal

# Defaults
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_BAUD_RATE = 115200
DEFAULT_ZMQ_PORT = 4020
GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947
RECONNECT_DELAY = 5  # seconds

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
    log_level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=log_level,
                        format="%(asctime)s [%(levelname)s] %(message)s")

def init_gps_connection():
    """Initializes a persistent gpsd connection (socket + data_stream)."""
    gps_socket = gps3.GPSDSocket()
    data_stream = gps3.DataStream()
    try:
        gps_socket.connect(host=GPSD_HOST, port=GPSD_PORT)
        gps_socket.watch()
        logging.info("GPS connection established.")
    except Exception as e:
        logging.error("Failed to initialize GPS connection: %s", e)
        gps_socket, data_stream = None, None
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
    except Exception as e:
        logging.error("Error getting GPS location: %s", e)
    return 0.0, 0.0

def read_serial(serial_port, baud_rate):
    """Continuously attempts to connect and read from the serial port."""
    while True:
        try:
            with serial.Serial(serial_port, baud_rate, timeout=1) as ser:
                logging.info("Connected to %s at %d baud.", serial_port, baud_rate)
                while True:
                    line = ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        logging.debug("Raw data: %s", line)
                        yield line
        except serial.SerialException as e:
            logging.error("Serial connection error: %s. Reconnecting in %d seconds...", e, RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)
        except Exception as e:
            logging.exception("Unexpected error while reading serial port: %s", e)
            time.sleep(RECONNECT_DELAY)

def process_message(data):
    """
    Extracts key elements from the message, including source node information.
    
    Returns a structured dictionary containing:
      - source_node: the node ID from which the message originated
      - message_type: 'nodeMsg' or 'nodeAlert'
      - status: status string (e.g., 'NEW CONTACT LOCK')
      - time, rssi, freq, var, data: additional key data if present
    """
    try:
        msg = data.get("msg", {})
        source = data.get("from", {}).get("node", "unknown")
        processed = {
            "source_node": source,
            "message_type": msg.get("type", ""),
            "status": msg.get("stat", ""),
            "time": msg.get("time"),
            "rssi": msg.get("rssi"),
            "freq": msg.get("freq"),
            "var": msg.get("var"),
            "data": msg.get("data")
        }
        return processed
    except Exception as e:
        logging.error("Error processing message: %s", e)
        return {}

def signal_handler(sig, frame):
    logging.info("Signal %s received, exiting...", sig)
    sys.exit(0)

def main():
    args = parse_args()
    setup_logging(args.debug)
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Initialize GPS connection and cache location if stationary
    gps_socket, data_stream = None, None
    lat_cache, lon_cache = 0.0, 0.0

    if not args.stationary:
        gps_socket, data_stream = init_gps_connection()
    else:
        gps_socket, data_stream = init_gps_connection()
        if gps_socket and data_stream:
            lat_cache, lon_cache = get_gps_location(gps_socket, data_stream)
            gps_socket.close()
            gps_socket, data_stream = None, None
        else:
            logging.error("GPS initialization failed in stationary mode. Using default coordinates.")

    # Setup ZMQ XPUB socket for message publication
    context = zmq.Context()
    zmq_socket = context.socket(zmq.XPUB)
    zmq_socket.setsockopt(zmq.SNDHWM, 1000)  # High-water mark for buffering
    zmq_socket.setsockopt(zmq.LINGER, 0)      # Ensure clean socket shutdown
    zmq_endpoint = f"tcp://0.0.0.0:{args.zmq_port}"
    try:
        zmq_socket.bind(zmq_endpoint)
        logging.info("ZMQ XPUB socket bound to %s", zmq_endpoint)
    except zmq.ZMQError as e:
        logging.error("Failed to bind ZMQ socket: %s", e)
        sys.exit(1)

    try:
        # Process and publish messages continuously from the serial port.
        for line in read_serial(args.serial, args.baud):
            try:
                raw_data = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("Failed to parse JSON: %s", line)
                continue

            processed_msg = process_message(raw_data)
            if not processed_msg:
                continue

            # Handle message types and log details
            message_type = processed_msg.get("message_type", "")
            status = processed_msg.get("status", "")
            if message_type == "nodeMsg":
                if status.startswith("NODE_START"):
                    logging.info("Boot message received from node %s: %s",
                                 processed_msg["source_node"], status)
                elif "CALIBRATION COMPLETE" in status:
                    logging.info("Calibration complete from node %s: %s",
                                 processed_msg["source_node"], status)
                else:
                    logging.debug("nodeMsg from node %s: %s",
                                  processed_msg["source_node"], status)
            elif message_type == "nodeAlert":
                if "NEW CONTACT LOCK" in status:
                    logging.warning("New FPV Drone detected from node %s!",
                                    processed_msg["source_node"])
                elif "LOCK UPDATE" in status:
                    logging.info("Lock update from node %s.",
                                 processed_msg["source_node"])
                elif "LOST CONTACT LOCK" in status:
                    logging.warning("Lost contact lock from node %s.",
                                    processed_msg["source_node"])
                else:
                    logging.debug("nodeAlert from node %s: %s",
                                  processed_msg["source_node"], status)
                if processed_msg.get("rssi") is not None:
                    logging.debug("RSSI: %s, Frequency: %s",
                                  processed_msg.get("rssi"), processed_msg.get("freq"))

            # Update GPS location if applicable
            if not args.stationary and gps_socket and data_stream:
                current_lat, current_lon = get_gps_location(gps_socket, data_stream)
                lat, lon = current_lat, current_lon
            else:
                lat, lon = lat_cache, lon_cache

            # Attach GPS coordinates to the processed message
            processed_msg["gps_lat"] = lat
            processed_msg["gps_lon"] = lon

            # Publish the structured JSON message via ZMQ
            try:
                json_message = json.dumps(processed_msg)
                zmq_socket.send_string(json_message)
                logging.debug("Published: %s", json_message)
            except Exception as e:
                logging.error("Error publishing message via ZMQ: %s", e)
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received. Shutting down...")
    except Exception as e:
        logging.exception("Unexpected error in main loop: %s", e)
    finally:
        # Clean up and close resources gracefully
        try:
            zmq_socket.close(0)
            context.term()
            logging.info("ZMQ socket and context terminated.")
        except Exception as e:
            logging.error("Error during ZMQ cleanup: %s", e)
        if gps_socket:
            try:
                gps_socket.close()
            except Exception as e:
                logging.error("Error closing GPS socket: %s", e)
        logging.info("Shutdown complete.")

if __name__ == "__main__":
    main()
