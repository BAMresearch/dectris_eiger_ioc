import socket
from pathlib import Path
import os

# Validators for IP and Port
def validate_ip_address(instance, attribute, value):
    try:
        socket.inet_aton(value)
    except socket.error:
        raise ValueError(f"Invalid IP address: {value}")


def validate_port_number(instance, attribute, value):
    if not (0 <= value <= 65535):
        raise ValueError(f"Port number must be between 0 and 65535, got {value}")

def ensure_directory_exists_and_is_writeable(instance, attribute, value):
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)  # Create the directory if it doesn't exist

    if not path.is_dir():
        raise ValueError(f"The directory '{value}' does not exist.")
    if not os.access(path, os.W_OK):
        raise ValueError(f"The directory '{value}' is not writable.")
    