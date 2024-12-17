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

- Python 3.11 or higher
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
- `--localPath`: The directory path where files will be (retrieved and) stored locally after data collection.

To start a capture, you can use the following (granular) steps:
1. Restart the detector, changes DetectorState to 'na' or 'unknown', and resets internal exposure ID counter. Should only have to be done infrequently.
```bash
caproto-put <prefix:>Restart True 
captoro-get <prefix:>Restart_RBV
```
RBV value will return to False/Off when restart is complete.

2. Iniitalize the detector, should change DetectorState to 'idle'
```bash
caproto-put <prefix:>Initialize True
captoro-get <prefix:>Initialize_RBV
```
RBV value will return to False/Off when initialization is complete, might take a few seconds. 

3. Change detector and timing parameters as required
```bash
caproto-put <prefix:>CountTime 3.1234
caproto-put <prefix:>FrameTime 1
caproto-put <prefix:>PhotonEnergy 8050
caproto-put <prefix:>ThresholdEnergy 4025
...
```

4. Configure the detector parameters, applies all the settings to the detector and filewriter settings. This should be done after every parameter change
```bash
caproto-put <prefix:>Configure True
captoro-get <prefix:>Configure_RBV
```
RBV value will return to False/Off when configuration is complete, might take a few seconds. 


5. Trigger an exposure. After exposure is complete, this will retrieve the files into the location you specified. 
```bash
caproto-put <prefix:>Trigger True
captoro-get <prefix:>Trigger_RBV
```
RBV value will return to False/Off when exposure and file retrieval is complete. 

You can trigger as many exposures by repeating the last command as needed, as long as the exposure parameters are not changed. The first exposure will take some time as the detector needs to do some housekeeping before (detectorstate "configuring"), but the subsequent exposures will be quicker. 


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
