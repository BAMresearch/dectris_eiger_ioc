
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
from typing import Callable

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
    
    See README.md for information on how to use it. 

    Attributes:
        host (str): IP address of the detector.
        port (int): Port number for the detector connection.
        client (DEigerClient): Client interface for communicating with the detector.
        LocalFileDumpPath (Path): Path where the detector files are stored locally.
        _nframes (int): Number of frames to be taken in a single exposure.
        _starttime (datetime): Start time of the exposure.
        
    authors: Brian R. Pauw, Anja Hörmann. 
    DEigerClient from Dectris
    License: MIT    
    """

    host: str = attrs.field(default="172.17.1.2", validator=validate_ip_address, converter=str)
    port: int = attrs.field(default=80, validator=validate_port_number, converter=int)
    client: DEigerClient = attrs.field(init=False, validator=attrs.validators.optional(attrs.validators.instance_of(DEigerClient)))
    # files measured on the detector are stored here. 
    LocalFileDumpPath: Path = attrs.field(default=Path("/tmp"), converter=Path, validator=[attrs.validators.instance_of(Path), ensure_directory_exists_and_is_writeable])
    # number of frames to be taken in a single exposure
    _nframes: int = attrs.field(default=1, validator=attrs.validators.optional(attrs.validators.instance_of(int)))
    _nimages_per_file:int = attrs.field(default=1800, validator=attrs.validators.instance_of(int))
    # start time of the exposure
    _starttime: datetime = attrs.field(default=None, validator=attrs.validators.optional(attrs.validators.instance_of(datetime)))
    # for any location-specific operations that need to be performed after data collection
    custom_post_exposure_operation: CustomPostExposureOperation = attrs.field(factory=CustomPostExposureOperation)
    # if we tried writing while the detector was initializing or measuring:
    _detector_initialized:bool = attrs.field(default=False, validator=attrs.validators.instance_of(bool))
    _detector_configured:bool = attrs.field(default=False, validator=attrs.validators.instance_of(bool))
    _communications_lock: asyncio.Lock = attrs.field(factory=asyncio.Lock)

    def __init__(self, *args, **kwargs) -> None:
        for k in list(kwargs.keys()):
            if k in ['host', 'port']:
                setattr(self, k, kwargs.pop(k))
        self.LocalFileDumpPath = kwargs.pop('localPath', Path("/tmp"))
        print(f'{self.LocalFileDumpPath=}')
        self.client = DEigerClient(self.host, port=self.port)
        self._starttime = None #datetime.now(timezone.utc)
        self._nimages_per_file = 1800
        self._detector_initialized = False
        self._communications_lock = asyncio.Lock()
        self._nframes = 0
        super().__init__(*args, **kwargs)

    def empty_data_store(self):
        self.client.sendFileWriterCommand("clear")
        # writing of files needs to be enabled again after
        self.client.setFileWriterConfig("mode", "enabled")

    def restart_detector(self):
        print("  restarting detector")        
        self.client.sendSystemCommand("restart")
        time.sleep(.1)

    def initialize_detector(self):
        print("  sending init command")
        self._detector_initialized = False
        Trouble=False
        try:
            self.client.sendDetectorCommand("initialize")
            print("  finished sending init command")
        except RuntimeError as e:
            print(f"  Trouble initializing, RunTimeError received: {e}")
            Trouble=True
        ntry = 5
        while (self.DetectorState.value in ['na', 'error']) or Trouble:
            print(f'failure to initialize detector, trying again {ntry} times out of 5')
            print(f'{self.DetectorState.value =}, {Trouble =}')
            time.sleep(1)
            try:
                self.client.sendDetectorCommand("initialize")
                print("  finished sending init command")
                Trouble=False
            except RuntimeError as e:
                print(f"  Trouble initializing, RunTimeError received: {e}")
                Trouble=True
            ntry -=1
            if ntry <0:
                print(f'FAILURE TO INITIALIZE detector')
                return
                
        self._detector_initialized = True
        return


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
        print("frame_time to be set: ", FrameTime)
        self.client.setDetectorConfig("frame_time", FrameTime) 
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

        if not self._detector_initialized:
            print('before configuring the detector, I will intialize it at least once...')
            self.initialize_detector()
        self.set_energy_values()
        self.set_timing_values()
        self.empty_data_store()
        self.set_filewriter_config()
        self.client.setDetectorConfig("countrate_correction_applied", self.CountRateCorrection.value)
        self.client.setDetectorConfig("flatfield_correction_applied", self.FlatFieldCorrection.value)
        self.client.setDetectorConfig("pixel_mask_applied", self.PixelMaskCorrection.value)        

    def read_detector_configuration_safely(self, key:str="", default=None, readMethod: str = 'detectorStatus'):
        """ reads the detector configuration of a particular key and returns it as a dictionary. Safely handles errors"""
        try:
            if readMethod == 'detectorStatus':
                answer = self.client.detectorStatus(key)
            else: 
                answer = self.client.detectorConfig(key)

            if not isinstance(answer, dict):
                return default
            else:
                return answer["value"]
        except:
            return default

    def read_and_dump_files(self):
        """ reads all files in the data store and dumps them to disk at the location specified upon IOC init"""

        expected_number_of_files = np.ceil(self._nframes/self._nimages_per_file)+1
        
        filenames = self.client.fileWriterFiles()# returns all files in datastore
        ntry = 200 # 20 seconds...
        while not len(filenames)>=expected_number_of_files:
            time.sleep(.1)
            filenames = self.client.fileWriterFiles() #['value'] # returns all files in datastore
            if ntry <0:
                print('did not find the needed number of files after 20 seconds')
                return 
            ntry -= 1

        print(f'filenames found: {filenames}')
        for filename in filenames:
            if (filename in os.listdir(self.LocalFileDumpPath)) or not filename.startswith(self.OutputFilePrefix.value):
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

    async def wait_for_init_complete(self):
        reconfigure = False
        if self.DetectorState.value in ['error']:
            print('error state detected in detector, restarting before reinitializing...')
            await self.Restart.write(True)
            await asyncio.sleep(2)
            reconfigure = True

        if self.DetectorState.value in ['na', 'error', 'ready']:
            print('error state detected in detector, reinitializing before triggering...')
            await self.Initialize.write(True)
            await asyncio.sleep(.1)
            reconfigure = True
            
        counter = 0
        while self.Initialize_RBV.value not in ['Off', False]:
            counter += 1
            await asyncio.sleep(.1)
            if (counter % 10 == 0):
                print('waiting for initialization to complete...')
            if counter>250:
                break

        if reconfigure: 
            await self.Configure.write(True)
            await asyncio.sleep(.1)
            reconfigure = False

        counter = 0
        while self.Configure_RBV.value not in ['Off', False]:
            counter += 1
            await asyncio.sleep(.1)
            if (counter % 10 == 0):
                print('waiting for configuration to complete...')
            if counter>250:
                break

    async def arm_trigger_disarm(self):
        print('arming detector')
        counter = 0  
        loop = asyncio.get_event_loop()
        # await loop.run_in_executor(None, self.initialize_detector)
        while counter <10:
            counter += 1
            try:
                await asyncio.sleep(.1)
                async with self._communications_lock:
                    arm_answer = await loop.run_in_executor(None, self.client.sendDetectorCommand, "arm")
                print(f'{arm_answer =}')
                if isinstance(arm_answer, dict):
                    if arm_answer.get('sequence id', -1) >= 0:
                        break # correct response, done if we got to this stage
            except RuntimeError:
                print(f'trouble arming detector in attempt {counter}, waiting a second before trying again')
                await asyncio.sleep(1)

        print('triggering detector')
        counter = 0
        self._starttime = datetime.now(timezone.utc)
        while counter <10:
            counter += 1
            try:
                # do not lock this or we'll be stuck for the duration of the exposure
                # async with self._communications_lock:
                await asyncio.sleep(.5)
                trigger_answer = await loop.run_in_executor(None, self.client.sendDetectorCommand, "trigger")
                print(f'{trigger_answer =}')
                if isinstance(trigger_answer, dict):
                    if trigger_answer.get('sequence id', 0) == -1:
                        break # correct response, done if we got to this stage
            except RuntimeError:
                print(f'trouble triggering detector in attempt {counter}, waiting a second before trying again')
                await asyncio.sleep(1)
        print('disarming detector')
        counter = 0
        while counter <10:
            counter += 1
            try:
                await asyncio.sleep(.1)
                async with self._communications_lock:
                    disarm_answer = await loop.run_in_executor(None, self.client.sendDetectorCommand, "disarm")
                print(f'{disarm_answer =}')
                if isinstance(disarm_answer, dict):
                    if disarm_answer.get('sequence id', -1) >= 0:
                        break # correct response, done if we got to this stage
            except RuntimeError:
                print(f'trouble disarming detector in attempt {counter}, waiting a second before trying again')
                await asyncio.sleep(1)


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
    Restart = pvproperty(doc="Restart the detector, resets to False immediately", dtype=bool, record='bi')
    Restart_RBV = pvproperty(doc="True while detector is restarting", dtype=bool, record='bo')
    Initialize = pvproperty(doc="Initialize the detector, resets to False immediately", dtype=bool, record='bi')
    Initialize_RBV = pvproperty(doc="True while detector is initializing", dtype=bool, record='bo')
    Configure = pvproperty(doc="Configures the detector, resets to False immediately", dtype=bool, record='bi')
    Configure_RBV = pvproperty(doc="True while detector is Configuring", dtype=bool, record='bo')
    Trigger = pvproperty(doc="Trigger the detector to take an image, resets to False immediately. Adjusts detector_state to 'busy' for the duration of the measurement.", record='bi', dtype=bool)
    Trigger_RBV = pvproperty(doc="True while the detector capture subroutine in the IOC is busy", dtype=bool, record='bo')
    OutputFilePrefix = pvproperty(value="eiger_", doc="Set the prefix of the main and data output files", dtype=str, record='stringin', report_as_string=True)
    LatestFile = pvproperty(value = '', doc="Shows the name of the latest output file retrieved", dtype=str, record='stringin', report_as_string=True)
    LatestFileData = pvproperty(value = '', doc="Shows the name of the latest output data file retrieved", dtype=str, record='stringin', report_as_string=True)
    LatestFileMain = pvproperty(value = '', doc="Shows the name of the latest output main file retrieved", dtype=str, record='stringin', report_as_string=True)
    SecondsRemaining = pvproperty(value = 0.0, doc="Shows the seconds remaining for the current exposure", dtype=float, record='ai')

    @DetectorState.scan(period=5, use_scan_field=True, subtract_elapsed=True)
    async def DetectorState(self, instance, async_lib):
        async with self._communications_lock:
            await self.DetectorState.write(self.read_detector_configuration_safely("state", "unknown", readMethod='detectorStatus'))

    @DetectorTemperature.scan(period=60, use_scan_field=True, subtract_elapsed=True)
    async def DetectorTemperature(self, instance, async_lib):
        async with self._communications_lock:
            await self.DetectorTemperature.write(float(self.read_detector_configuration_safely("board_000/th0_temp", -999.0, readMethod='detectorStatus')))

    @SecondsRemaining.scan(period=1, use_scan_field=True, subtract_elapsed=True)
    async def SecondsRemaining(self, instance, async_lib):
        if self._starttime is not None:
            elapsed = datetime.now(timezone.utc) - self._starttime
            remaining = self.CountTime.value - elapsed.total_seconds()
            await self.SecondsRemaining.write(int(np.maximum(remaining, 0)))
        else:
            await self.SecondsRemaining.write(-999)

    @DetectorTime.getter
    async def DetectorTime(self, instance):
        async with self._communications_lock:
            await self.DetectorTime.write(self.read_detector_configuration_safely("time", "unknown", readMethod='detectorStatus'))

    @CountTime_RBV.getter
    async def CountTime_RBV(self, instance):
        async with self._communications_lock:
            await self.CountTime_RBV.write(float(self.read_detector_configuration_safely("count_time", -999.0, readMethod='detectorConfig')))

    @CountTime.getter
    async def CountTime(self, instance):
        async with self._communications_lock:
            await self.CountTime_RBV.write(float(self.read_detector_configuration_safely("count_time", -999.0, readMethod='detectorConfig')))

    @FrameTime_RBV.getter
    async def FrameTime_RBV(self, instance):
        async with self._communications_lock:
            await self.FrameTime_RBV.write(float(self.read_detector_configuration_safely("frame_time", -999.0, readMethod='detectorConfig')))

    @FrameTime.getter
    async def FrameTime(self, instance):
        async with self._communications_lock:
            await self.FrameTime_RBV.write(float(self.read_detector_configuration_safely("frame_time", -999.0, readMethod='detectorConfig')))


    @Initialize.putter
    async def Initialize(self, instance, value: bool):
        # await self.ReadyToTrigger.write(False)
        if value:
            await self.Initialize_RBV.write(True)
            loop = asyncio.get_event_loop()
            print('Initializer running self.initialize_detector')
            async with self._communications_lock:
                await loop.run_in_executor(None, self.initialize_detector)
            await self.Initialize_RBV.write(False)

    @Configure.putter
    async def Configure(self, instance, value: bool):
        if value:
            await self.Configure_RBV.write(True)
            loop = asyncio.get_event_loop()
            async with self._communications_lock:
                await loop.run_in_executor(None, self.configure_detector)
            await self.Configure_RBV.write(False)

    @Restart.putter
    async def Restart(self, instance, value: bool):
        if value:
            await self.Restart_RBV.write(True)
            loop = asyncio.get_event_loop()
            async with self._communications_lock:
                await loop.run_in_executor(None, self.restart_detector)
            await self.Restart_RBV.write(False)

    @Trigger.putter
    async def Trigger(self, instance, value: bool):
        if value:
            await self.Trigger_RBV.write(True)
            # ensure initialisation is complete first..
            print('running wait_for_init_complete()')
            await self.wait_for_init_complete()
            loop = asyncio.get_event_loop()
            # this one has locks in it
            await self.arm_trigger_disarm()
            print('retrieving files')
            async with self._communications_lock:
                await loop.run_in_executor(None, self.retrieve_all_and_clear_files)
            print('done retrieving files')
            await self.Trigger_RBV.write(False)
            
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

    ioc = DEigerIOC(host=args.host, port=args.port, localPath=args.localPath, **ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
