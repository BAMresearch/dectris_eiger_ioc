
import logging
from pathlib import Path
import socket
import sys
import attrs
from datetime import datetime, timezone
from caproto.server import PVGroup, pvproperty, PvpropertyString, run, template_arg_parser, AsyncLibraryLayer
from caproto import ChannelData
import numpy as np
from deigerclient import DEigerClient
import os 

import logging

logger = logging.getLogger("DEigerIOC")
logger.setLevel(logging.INFO)

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
    
@attrs.define
class DEigerIOC(PVGroup):
    """
    A caproto-based IOC (Input/Output Controller) for managing Dectris Eiger detectors.

    This class facilitates setting, triggering, and collecting data from Dectris Eiger detectors.
    It integrates network configuration, data handling, and detector management by wrapping
    various detector functionalities including energy values, timing configuration, and file writing.

    Attributes:
        host (str): IP address of the detector.
        port (int): Port number for the detector connection.
        client (DEigerClient): Client interface for communicating with the detector.
        LocalFileDumpPath (Path): Path where the detector files are stored locally.
        _nframes (int): Number of frames to be taken in a single exposure.
        _starttime (datetime): Start time of the exposure.
        
    Methods:
        empty_data_store: Clears the detector's data store.
        restart_and_initialize_detector: Restarts and initializes the detector.
        set_energy_values: Sets photon energy and energy threshold values.
        set_timing_values: Configures count time and frame time for the detector.
        set_filewriter_config: Enables and configures the file writer for data output.
        set_monitor_and_stream_config: Configures monitor and stream settings.
        configure_detector: Runs required initializations before a measurement.
        read_detector_configuration_safely: Reads detector configuration safely while handling errors.

    """

    host: str = attrs.field(default="172.17.1.2", validator=validate_ip_address, converter=str)
    port: int = attrs.field(default=80, validator=validate_port_number, converter=int)
    client: DEigerClient = attrs.field(init=False, validator=attrs.validators.optional(attrs.validators.instance_of(DEigerClient)))
    # files measured on the detector are stored here. 
    LocalFileDumpPath: Path = attrs.field(default=Path("/tmp"), validator=[attrs.validators.instance_of(Path), ensure_directory_exists_and_is_writeable])
    # number of frames to be taken in a single exposure
    _nframes: int = attrs.field(default=1, validator=attrs.validators.optional(attrs.validators.instance_of(int)))
    # start time of the exposure
    _starttime: datetime = attrs.field(default=datetime.now(timezone.utc), validator=attrs.validators.optional(attrs.validators.instance_of(datetime)))

    def __init__(self, *args, **kwargs) -> None:
        for k in list(kwargs.keys()):
            if k in ['host', 'port']:
                setattr(self, k, kwargs.pop(k))
        self.client = DEigerClient(self.host, port=self.port)
        super().__init__(*args, **kwargs)

    def empty_data_store(self):
        self.client.sendFileWriterCommand("clear")
        # writing of files needs to be enabled again after
        self.client.setFileWriterConfig("mode", "enabled")

    def restart_and_initialize_detector(self):
        logging.info("restarting detector")        
        self.client.sendSystemCommand("restart")
        self.client.sendDetectorCommand("initialize")

    def set_energy_values(self):
        self.client.setDetectorConfig("photon_energy", self.PhotonEnergy.value)
        self.client.setDetectorConfig("energy_threshold", self.EnergyThreshold.value)

    def set_timing_values(self):
        """ this also sets _nframes to the correct value"""
        self.client.setDetectorConfig("count_time", self.CountTime.value)
        self.client.setDetectorConfig("frame_time", self.FrameTime.value)
        self._nframes = int(np.ceil(self.CountTime.value / self.FrameTime.value))
        self.client.setDetectorConfig("nimages",self._nframes)

    def set_filewriter_config(self):
        self.client.setFileWriterConfig("mode", "enabled")
        self.client.setFileWriterConfig("name_pattern", f"{self.OutputFilePrefix.value}$id")
        self.client.fileWriterConfig("compression_enabled")
        self.client.setDetectorConfig("compression", "bslz4")
    
    def set_monitor_and_stream_config(self):
        self.client.monitorConfig("mode","disabled")
        # zmq stream config
        # self.client.setStreamConfig('format','cbor')
        self.client.setStreamConfig("mode","disabled")
        # self.client.setStreamConfig("header_detail", "all")

    def configure_detector(self):     
        """ runs all the required detector initializations before a measurement"""   
        # self.restart_and_initialize_detector() # not sure this is necessary every time
        self.set_energy_values()
        self.set_timing_values()
        self.empty_data_store()
        self.set_filewriter_config()

        self.client.setDetectorConfig("trigger_mode","ints")

    def read_detector_configuration_safely(self, key:str="", default=None):
        """ reads the detector configuration of a particular key and returns it as a dictionary. Safely handles errors"""
        try:
            answer = self.client.detectorStatus(key)
            if not isinstance(answer, dict):
                return default
            else:
                return answer["value"]
        except:
            return default

    def read_and_dump_files(self):
        """ reads all files in the data store and dumps them to disk at the location specified upon IOC init"""
        # TODO: check how this writes files during measurememnt - does it update as files are created, or does it create files only on completion?
        filenames = self.client.fileWriterFiles()['value'] # returns all files in datastore
        for filename in filenames:
            if filename in os.listdir(self.LocalFileDumpPath) or not filename.startswith(self.OutputFilePrefix.value):
                continue # skip if file already exists or is one we're not looking for
            self.client.FileWriterSave(filename, self.LocalFileDumpPath)
            self.LatestFile=str(filename)

    def retrieve_all_and_clear_files(self):
        """ retrieves all files from the data store and clears the data store"""
        self.read_and_dump_files()
        self.empty_data_store()

    # Detector state readouts
    DetectorState = pvproperty(doc="State of the detector, can be 'busy' or 'idle'", dtype=str, record='stringin')
    DetectorTemperature = pvproperty(doc="Temperature of the detector", dtype=float, record='ai')
    DetectorTime = pvproperty(doc="Timestamp on the detector", dtype=str, record='stringin')
    CountTime_RBV = pvproperty(doc="Gets the actual total exposure time the detector", dtype=float, record='ai')
    FrameTime_RBV = pvproperty(doc="Gets the actual frame time from the detector", dtype=float, record='ai')

    # settables for the detector
    EnergyThreshold = pvproperty(doc="Sets the energy threshold on the detector, normally 0.5 * PhotonEnergy", dtype=float, record='ao')
    PhotonEnergy = pvproperty(value = 8050, doc="Sets the photon energy on the detector", dtype=int, record='ai')
    FrameTime = pvproperty(doc="Sets the frame time on the detector. nominally should be <= CountTime", dtype=float, record='ai')
    CountTime = pvproperty(doc="Sets the total exposure time the detector", dtype=float, record='ai')
    CountRateCorrection = pvproperty(doc="do you want count rate correction applied by the detector (using int maths)", dtype=bool, record='bo')
    FlatFieldCorrection = pvproperty(doc="do you want flat field correction applied by the detector (using int maths)", dtype=bool, record='bo')

    # operating the detector
    Initialize: bool = pvproperty(doc="Initialize the detector, resets to False immediately", dtype=bool, record='bo')
    Trigger: bool = pvproperty(doc="Trigger the detector to take an image, resets to False immediately. Adjusts detector_state to 'busy' for the duration of the measurement.", dtype=bool, record='bo')
    Trigger_RBV: bool = pvproperty(doc="True while the detector capture subroutine in the IOC is busy", dtype=bool, record='bo')
    OutputFilePrefix = pvproperty(value="eiger_", doc="Set the prefix of the main and data output files", dtype=str, record='stringin')
    LatestFile = pvproperty(doc="Shows the name of the latest output file retrieved", dtype=str, record='stringin')
    SecondsRemaining = pvproperty(doc="Shows the seconds remaining for the current exposure", dtype=int, record='longin')
    FileScanner = pvproperty(doc="Scans the data store for new files and dumps them to disk", dtype=bool, record='bo')

    @FileScanner.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def FileScanner(self, instance, async_lib):
        self.read_and_dump_files()

    @DetectorState.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def DetectorState(self, instance, async_lib):
        await self.DetectorState.write(self.read_detector_configuration_safely("state", "unknown"))

    @DetectorTemperature.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def DetectorTemperature(self, instance, async_lib):
        await self.DetectorTemperature.write(float(self.read_detector_configuration_safely("temperature", -999.0)))

    @DetectorTime.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def DetectorTime(self, instance, async_lib):
        await self.DetectorTime.write(self.read_detector_configuration_safely("time", "unknown"))

    @SecondsRemaining.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def SecondsRemaining(self, instance, async_lib):
        if self._starttime is not None:
            elapsed = datetime.now(timezone.utc) - self._starttime
            remaining = self.CountTime.value - elapsed.total_seconds()
            await self.SecondsRemaining.write(int(remaining))
        else:
            await self.SecondsRemaining.write(-999)

    @CountTime_RBV.getter
    async def CountTime_RBV(self, instance):
        return self.read_detector_configuration_safely("count_time", -999.0)

    @EnergyThreshold.putter
    async def EnergyThreshold(self, instance, value: float):
        self.set_energy_values()

    @PhotonEnergy.putter
    async def PhotonEnergy(self, instance, value: int):
        self.set_energy_values()

    @FrameTime.putter
    async def FrameTime(self, instance, value: float):
        self.set_timing_values()

    @CountTime.putter
    async def CountTime(self, instance, value: float):
        self.set_timing_values()    
    @CountTime.getter
    async def CountTime(self, instance):
        return self.read_detector_configuration_safely("count_time", -999.0)

    @Initialize.putter
    async def Initialize(self, instance, value: bool):
        if value:
            self.restart_and_initialize_detector()
            self.configure_detector()            
            await self.Initialize.write(False)

    @Trigger.putter
    async def Trigger(self, instance, value: bool):
        if value:
            await self.Trigger_RBV.write(True)
            await self.Trigger.write(False)
            self._starttime = datetime.now(timezone.utc)
            self.client.sendDetectorCommand("arm")
            # TODO: check if this works:
            await self.client.sendDetectorCommand("trigger")
            self.client.sendDetectorCommand("disarm")
            self.retrieve_all_and_clear_files()
            await self.Trigger_RBV.write(False)
            

def main(args=None):
    parser, split_args = template_arg_parser(
        default_prefix="detector_eiger:",
        desc="EPICS IOC for setting and capturing images using the Dectris Eiger detector",
    )

    if args is None:
        args = sys.argv[1:]

    parser.add_argument("--host", required=True, type=str, help="IP address of the host/device")
    parser.add_argument("--port", required=True, type=int, help="Port number of the device")
    parser.add_argument("--local-file-dump-path", type=Path, default=Path("/tmp"),
                    help="Path where the detector files are stored locally")

    args = parser.parse_args()

    logging.info(f"Running Dectis Eiger IOC on {args}")

    ioc_options, run_options = split_args(args)

    # Remove local_file_dump_path from ioc_options if not needed further
    ioc_options.pop('local_file_dump_path', None)

    ioc = DEigerIOC(host=args.host, port=args.port, LocalFileDumpPath=args.local_file_dump_path, **ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
