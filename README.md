# Dectris Eiger IOC

This repository provides a simple caproto-based IOC (Input/Output Controller) used for setting, triggering, and collecting data to files from Dectris Eiger detectors. This controller is designed to be flexible and customizable for use in different operational settings. For the moment, only internally triggered series capture is supported. 

## Project Structure

- **config.py**: Handles command-line argument parsing and configuration setup.
- **dectris_eiger_ioc.py**: Contains the main `DEigerIOC` class which manages interaction with the Dectris Eiger detectors, defining process variables (PVs) and performing key operations. Also acts as the entry point of the application, initiating the IOC and starting the server.
- **custom_operations.py**: Defines `CustomPostExposureOperation` classes used for location-specific or custom operations to be performed after data collection.
- **validators.py**: Includes utility functions to validate inputs such as IP addresses, port numbers, and directory paths.
- **deigerclient.py**: Provides a client interface to interact with the EIGER API. Provided by Dectris. 

## Getting Started

### Prerequisites

- Python 3.11+
- Required Python packages (as listed in `requirements.txt` or installable via an appropriate package manager).

### Setup

1. Clone this repository to your local environment:
   ```bash
   git clone https://github.com/yourusername/dectris_eiger_ioc.git
   cd dectris_eiger_ioc
   ```

2. Install necessary dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Running the IOC

To run the IOC, use the following command-line options:

```bash
python main.py --host <IP_ADDRESS> --port <PORT_NUMBER> --localPath <PATH>
```

- `--host`: The IP address of the Dectris Eiger detector.
- `--port`: The port number for communication with the detector. 80 by default.
- `--localPath`: The directory path where files will be stored locally after data collection.

### Custom Operations

To implement custom operation logic, extend the `CustomPostExposureOperation` in `operations.py` and pass it to the `DEigerIOC` when initializing.

```python
from custom_operations import CustomPostExposureOperation
from ioc import DEigerIOC

# Example of initializing the IOC with a custom operation
custom_operation = CustomPostExposureOperation()
ioc = DEigerIOC(custom_post_exposure_operation=custom_operation)
```

## Contributing

Contributions to this project are welcome. Please fork the repository and submit pull requests with any improvements or bug fixes.

## License

This project is licensed under the MIT License. See the LICENSE file for more details.
