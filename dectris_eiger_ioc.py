
import logging
from pathlib import Path
import sys
import attrs
from datetime import datetime, timezone
from caproto.server import PVGroup, pvproperty, template_arg_parser, run
import numpy as np
from deigerclient import DEigerClient
import os 
import asyncio
from validators import validate_ip_address, validate_port_number, ensure_directory_exists_and_is_writeable
from custom_operations import CustomPostExposureOperation
import time

import logging

logger = logging.getLogger("DEigerIOC")
# logger.setLevel(logging.INFO)

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

    authors: Brian R. Pauw, Anja Hörmann. 
    DEigerClient from Dectris
    License: MIT    
    """

    host: str = attrs.field(default="172.17.1.2", validator=validate_ip_address, converter=str)
    port: int = attrs.field(default=80, validator=validate_port_number, converter=int)
    client: DEigerClient = attrs.field(init=False, validator=attrs.validators.optional(attrs.validators.instance_of(DEigerClient)))
    # files measured on the detector are stored here. 
    LocalFileDumpPath: Path = attrs.field(default=Path("/tmp"), validator=[attrs.validators.instance_of(Path), ensure_directory_exists_and_is_writeable])
    # number of frames to be taken in a single exposure
    _nframes: int = attrs.field(default=1, validator=attrs.validators.optional(attrs.validators.instance_of(int)))
    _nimages_per_file:int = attrs.field(default=1800, validator=attrs.validators.instance_of(int))
    # start time of the exposure
    _starttime: datetime = attrs.field(default=None, validator=attrs.validators.optional(attrs.validators.instance_of(datetime)))
    # for any location-specific operations that need to be performed after data collection
    custom_post_exposure_operation: CustomPostExposureOperation = attrs.field(factory=CustomPostExposureOperation)
    # if we tried writing while the detector was initializing or measuring:
    _parameters_not_applied:bool = attrs.field(default=False, validator=attrs.validators.instance_of(bool))
    _detector_initialized:bool = attrs.field(default=False, validator=attrs.validators.instance_of(bool))

    def __init__(self, *args, **kwargs) -> None:
        for k in list(kwargs.keys()):
            if k in ['host', 'port']:
                setattr(self, k, kwargs.pop(k))
        self.LocalFileDumpPath = kwargs.pop('localPath', Path("/tmp"))
        self.client = DEigerClient(self.host, port=self.port)
        super().__init__(*args, **kwargs)
        self._starttime = None #datetime.now(timezone.utc)
        self._nimages_per_file = 1800
        self._detector_initialized = False
        # self._parameters_not_applied = False

    def empty_data_store(self):
        self.client.sendFileWriterCommand("clear")
        # writing of files needs to be enabled again after
        self.client.setFileWriterConfig("mode", "enabled")

    def restart_and_initialize_detector(self):
        logging.info("restarting detector")        
        self.client.sendSystemCommand("restart")
        time.sleep(.1)
        self.client.sendDetectorCommand("initialize")
        ntry = 5
        while self.DetectorState.value in ['na', 'error']:
            print(f'failure to initialize detector, trying again {ntry} times out of 5')
            time.sleep(1)
            self.client.sendDetectorCommand("initialize")
            ntry -=1
            if ntry <0:
                print(f'FAILURE TO INITIALIZE detector')
        self._detector_initialized = True


    def set_energy_values(self, PhotonEnergy = None, ThresholdEnergy = None):
        if PhotonEnergy is None:
            PhotonEnergy = self.PhotonEnergy.value
        if ThresholdEnergy is None:
            ThresholdEnergy = self.ThresholdEnergy.value
        self.client.setDetectorConfig("photon_energy", PhotonEnergy)
        self.client.setDetectorConfig("threshold_energy", ThresholdEnergy)

    def set_timing_values(self, FrameTime = None, CountTime = None):
        if FrameTime is None:
            FrameTime = self.FrameTime.value
        if CountTime is None:
            CountTime = self.CountTime.value
        """ this also sets _nframes to the correct value"""
        print("count_time to be set: ", CountTime)
        self.client.setDetectorConfig("count_time", CountTime)
        # don't set the frame time longer than count time.. 
        print("frame_time to be set: ", FrameTime)
        self.client.setDetectorConfig("frame_time", FrameTime) # np.minimum(self.FrameTime.value, self.CountTime.value))
        # maybe something else needs to be added here to account for deadtime between frames. 
        self._nframes = int(np.ceil(CountTime/ FrameTime))
        self.client.setDetectorConfig("nimages",self._nframes)
        self.client.setDetectorConfig("ntrigger", 1) # one trigger per sequence. (trigger_mode = ints)
        self.client.setDetectorConfig("trigger_mode","ints") # as seen in the dectris example notebook

    def set_filewriter_config(self):
        self.client.setFileWriterConfig("mode", "enabled") # write HDF5 files
        self.client.setFileWriterConfig("name_pattern", f"{self.OutputFilePrefix.value}$id")
        self.client.setFileWriterConfig("nimages_per_file", self._nimages_per_file) # maximum 1800 frames per file
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

        if not self._detector_initialized:
            print('before configuring the detector, I will intialize it at least once...')
            self.restart_and_initialize_detector()
        self.set_energy_values()
        self.set_timing_values()
        self.empty_data_store()
        self.set_filewriter_config()
        self.client.setDetectorConfig("countrate_correction_applied", self.CountRateCorrection.value)
        self.client.setDetectorConfig("flatfield_correction_applied", self.FlatFieldCorrection.value)
        self.client.setDetectorConfig("pixel_mask_applied", self.PixelMaskCorrection.value)        

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

        expected_number_of_files = np.ceil(self._nframes/self._nimages_per_file)+1
        # TODO: check how this writes files during measurememnt - does it update as files are created, or does it create files only on completion?
        
        filenames = self.client.fileWriterFiles()# returns all files in datastore
        ntry = 200 # 20 seconds...
        while not len(filenames)>=expected_number_of_files:
            time.sleep(.1)
            filenames = self.client.fileWriterFiles()#['value'] # returns all files in datastore
            if ntry <0:
                print('did not find the needed number of files after 20 seconds')
                return 
            ntry -= 1

        print(f'filenames found: {filenames}')
        for filename in filenames:
            if filename in os.listdir(self.LocalFileDumpPath) or not filename.startswith(self.OutputFilePrefix.value):
                continue # skip if file already exists or is one we're not looking for
            print(f'retrieving: {filename}')
            self.client.fileWriterSave(filename, self.LocalFileDumpPath)
            asyncio.run(self.LatestFile.write(str(filename)))
            if 'master' in filename:
                asyncio.run(self.LatestFileMain.write(str(filename)))
            elif 'data' in filename:
                asyncio.run(self.LatestFileData.write(str(filename)))

    def retrieve_all_and_clear_files(self):
        """ retrieves all files from the data store and clears the data store"""
        self.read_and_dump_files()
        self.empty_data_store()

    def ok_to_set_parameters(self)->bool:
        if self.DetectorState.value != 'idle':
            print('CANNOT SET PARAMETERS if detector is not idle')
            self._parameters_not_applied=True
            return False
            
        if self.Initialize_RBV.value not in ['Off', False]:
            print('CANNOT SET PARAMETERS if initalization is busy')
            self._parameters_not_applied=True
            return False

        return True

    async def ensure_parameters_applied(self):
        if self._parameters_not_applied:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.configure_detector)

    async def check_ready_to_trigger(self)->bool:
        await self.ensure_parameters_applied()
            
        if not self.ok_to_set_parameters():
            return False
        if not _detector_initialized:
            return False
        await self.ReadyToTrigger.write(True)

    async def wait_for_init_complete(self):
        counter = 0
        if self.DetectorState.value in ['na', 'error']:
            print('error state detected in detector, reinitializing before triggering...')
            await self.Initialize.write(True)
            await asyncio.sleep(.1)
            
        while self.Initialize_RBV.value not in ['Off', False]:
            counter += 1
            await asyncio.sleep(.1)
            if (counter % 10 == 0):
                print('waiting for initialization to complete...')

    # Detector state readouts
    DetectorState = pvproperty(value = '', doc="State of the detector, can be 'busy' or 'idle'", dtype=str, record='stringin',
                               report_as_string=True)
    DetectorTemperature = pvproperty(value = -999.9, doc="Temperature of the detector", dtype=float, record='ai')
    DetectorTime = pvproperty(value = '', doc="Timestamp on the detector", dtype=str, record='stringin', report_as_string=True)
    CountTime_RBV = pvproperty(value = 600.0, doc="Gets the actual total exposure time the detector", dtype=float, record='ai')
    FrameTime_RBV = pvproperty(value = 10.0, doc="Gets the actual frame time from the detector", dtype=float, record='ai')

    # settables for the detector
    ThresholdEnergy = pvproperty(value = 4025.0, doc="Sets the energy threshold on the detector, normally 0.5 * PhotonEnergy", record='ao')
    PhotonEnergy = pvproperty(value = 8050.0, doc="Sets the photon energy on the detector", record='ai')
    FrameTime = pvproperty(value = 10.0, doc="Sets the frame time on the detector. nominally should be <= CountTime", record='ai')
    CountTime = pvproperty(value = 600.0, doc="Sets the total exposure time the detector", record='ai')
    CountRateCorrection = pvproperty(value = True, doc="do you want count rate correction applied by the detector (using int maths)", record='bi')
    FlatFieldCorrection = pvproperty(value = False, doc="do you want flat field correction applied by the detector (using int maths)", record='bi')
    PixelMaskCorrection = pvproperty(value = False, doc="do you want pixel mask correction applied by the detector", record='bi')

    # operating the detector
    Initialize = pvproperty(doc="Initialize the detector, resets to False immediately", dtype=bool, record='bo')
    Initialize_RBV = pvproperty(value = False, doc="True while detector is initializing", dtype=bool, record='bo')
    Trigger = pvproperty(doc="Trigger the detector to take an image, resets to False immediately. Adjusts detector_state to 'busy' for the duration of the measurement.", record='bi')
    Trigger_RBV = pvproperty(value = False, doc="True while the detector capture subroutine in the IOC is busy", dtype=bool, record='bo')
    OutputFilePrefix = pvproperty(value="eiger_", doc="Set the prefix of the main and data output files", dtype=str, record='stringin', report_as_string=True)
    LatestFile = pvproperty(value = '', doc="Shows the name of the latest output file retrieved", dtype=str, record='stringin', report_as_string=True)
    LatestFileData = pvproperty(value = '', doc="Shows the name of the latest output data file retrieved", dtype=str, record='stringin', report_as_string=True)
    LatestFileMain = pvproperty(value = '', doc="Shows the name of the latest output main file retrieved", dtype=str, record='stringin', report_as_string=True)
    SecondsRemaining = pvproperty(value = 0, doc="Shows the seconds remaining for the current exposure", dtype=int, record='longin')
    ReadyToTrigger = pvproperty(value = False, doc="Set to False when not initialized, when setting parameters, etc. only True when truly ready. Remember to Initialize before Trigger", record='bi')


    @DetectorState.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def DetectorState(self, instance, async_lib):
        await self.DetectorState.write(self.read_detector_configuration_safely("state", "unknown"))

    @DetectorTemperature.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def DetectorTemperature(self, instance, async_lib):
        await self.DetectorTemperature.write(float(self.read_detector_configuration_safely("board_000/th0_temp", -999.0)))

    @DetectorTime.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def DetectorTime(self, instance, async_lib):
        await self.DetectorTime.write(self.read_detector_configuration_safely("time", "unknown"))

    @SecondsRemaining.scan(period=1, use_scan_field=True, subtract_elapsed=True)
    async def SecondsRemaining(self, instance, async_lib):
        if self._starttime is not None:
            elapsed = datetime.now(timezone.utc) - self._starttime
            remaining = self.CountTime.value - elapsed.total_seconds()
            await self.SecondsRemaining.write(int(np.maximum(remaining, 0)))
        else:
            await self.SecondsRemaining.write(-999)

    @CountTime_RBV.getter
    async def CountTime_RBV(self, instance):
        await self.CountTime_RBV.write(self.read_detector_configuration_safely("count_time", -999.0))

    @ThresholdEnergy.putter
    async def ThresholdEnergy(self, instance, value: float):
        await self.ReadyToTrigger.write(False)
        if self.ok_to_set_parameters():
            self.set_energy_values(ThresholdEnergy = value)
        await self.ReadyToTrigger.write(True)
        # await self.check_ready_to_trigger()

    @PhotonEnergy.putter
    async def PhotonEnergy(self, instance, value: float):
        await self.ReadyToTrigger.write(False)
        if self.ok_to_set_parameters():
            self.set_energy_values(PhotonEnergy = value)
        await self.ReadyToTrigger.write(True)
        # await self.check_ready_to_trigger()

    @FrameTime.putter
    async def FrameTime(self, instance, value: float):
        await self.ReadyToTrigger.write(False)
        if self.ok_to_set_parameters():
            self.set_timing_values(FrameTime = value)
        await self.ReadyToTrigger.write(True)
        # await self.check_ready_to_trigger()

    @CountTime.putter
    async def CountTime(self, instance, value: float):
        await self.ReadyToTrigger.write(False)
        if self.ok_to_set_parameters():
            self.set_timing_values(CountTime = value)    
        await self.ReadyToTrigger.write(True)
        # await self.check_ready_to_trigger()

    @CountTime.getter
    async def CountTime(self, instance):
        await self.CountTime_RBV.write(self.read_detector_configuration_safely("count_time", -999.0))

    @Initialize.putter
    async def Initialize(self, instance, value: bool):
        value=bool(value)
        await self.ReadyToTrigger.write(False)
        if value:
            await self.Initialize_RBV.write(True)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.restart_and_initialize_detector)
            await loop.run_in_executor(None, self.configure_detector)
            value=False
            await self.Initialize_RBV.write(False)
        await self.ReadyToTrigger.write(True)
        # await self.check_ready_to_trigger()
        # await self.Initialize.write(False)

    @Trigger.putter
    async def Trigger(self, instance, value: bool):
        value=bool(value)
        if value:
            await self.Trigger_RBV.write(True)
            await self.ReadyToTrigger.write(False)
            # ensure initialisation is complete first..
            await self.wait_for_init_complete()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.client.sendDetectorCommand, "arm")
            self._starttime = datetime.now(timezone.utc)
            await loop.run_in_executor(None, self.client.sendDetectorCommand, "trigger") # can this be done with await? it's not an async function...
            await loop.run_in_executor(None, self.client.sendDetectorCommand, "disarm")
            await loop.run_in_executor(None, self.retrieve_all_and_clear_files)
            await self.Trigger_RBV.write(False)
        await self.ReadyToTrigger.write(True)
        # await self.check_ready_to_trigger()
            
def main(args=None):
    parser, split_args = template_arg_parser(
        default_prefix="detector_eiger:",
        desc="EPICS IOC for setting and capturing images using the Dectris Eiger detector",
    )

    if args is None:
        args = sys.argv[1:]

    parser.add_argument("--host", required=True, type=str, help="IP address of the host/device")
    parser.add_argument("--port", type=int, default=80, help="Port number of the device")
    parser.add_argument("--localPath", "-L", type=Path, default=Path("/tmp"),
                    help="Path where the detector files are stored locally")

    args = parser.parse_args()

    logging.info(f"Running Dectis Eiger IOC on {args}")

    ioc_options, run_options = split_args(args)

    ioc = DEigerIOC(host=args.host, port=args.port, **ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
