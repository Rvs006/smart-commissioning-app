#!/usr/bin/env python3
"""Run the retained real-socket MQTT release gate for v0.1.47."""

import argparse
import unittest

from test_v0142_real_mqtt_socket import RealMqttSocketGate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18883)
    args, remainder = parser.parse_known_args()
    RealMqttSocketGate.host = args.host
    RealMqttSocketGate.port = args.port
    unittest.main(module="test_v0142_real_mqtt_socket", argv=[__file__, *remainder])


if __name__ == "__main__":
    main()
