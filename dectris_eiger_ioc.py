
import logging
import socket
import sys
import attrs
from datetime import datetime, timezone
from caproto.server import PVGroup, pvproperty, PvpropertyString, run, template_arg_parser, AsyncLibraryLayer
from caproto import ChannelData
import numpy as np
from deigerclient import DEigerClient

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


@attrs.define
class DEigerIOC(PVGroup):
    host: str = attrs.field(default="172.17.1.124", validator=validate_ip_address, converter=str)
    port: int = attrs.field(default=502, validator=validate_port_number, converter=int)
    client: DEigerClient = attrs.field(init=False, validator=attrs.validators.optional(attrs.validators.instance_of(DEigerClient)))
    # sets hdf5 compression on storage
    compression: str = attrs.field(default="gzip", validator=attrs.validators.optional(attrs.validators.instance_of(str)))
    compressionlevel: int = attrs.field(default=5, validator=attrs.validators.optional(attrs.validators.instance_of(int)))    
    _nframes: int = attrs.field(default=1, validator=attrs.validators.optional(attrs.validators.instance_of(int)))
    # start time of the exposure
    _starttime: datetime = attrs.field(default=datetime.now(timezone.utc), validator=attrs.validators.optional(attrs.validators.instance_of(datetime)))

    def __init__(self, *args, **kwargs) -> None:
        for k in list(kwargs.keys()):
            if k in ['host', 'port']:
                setattr(self, k, kwargs.pop(k))
        self.client = DEigerClient(self.host, self.port)
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

    # PVs
    # PVs for the detector
    DetectorState = pvproperty(doc="State of the detector, can be 'busy' or 'idle'", dtype=str, record='stringin')
    DetectorTemperature = pvproperty(doc="Temperature of the detector", dtype=float, record='ai')
    DetectorTime = pvproperty(doc="Timestamp on the detector", dtype=str, record='stringin')
    EnergyThreshold = pvproperty(doc="Sets the energy threshold on the detector, normally 0.5 * PhotonEnergy", dtype=float, record='ao')
    PhotonEnergy = pvproperty(value = 8050, doc="Sets the photon energy on the detector", dtype=int, record='ai')
    FrameTime = pvproperty(doc="Sets the frame time on the detector. nominally should be <= CountTime", dtype=float, record='ai')
    CountTime = pvproperty(doc="Sets the total exposure time the detector", dtype=float, record='ai')
    CountTime_RBV = pvproperty(doc="Gets the actual total exposure time the detector", dtype=float, record='ai')
    CountRateCorrection = pvproperty(doc="do you want count rate correction applied by the detector (using int maths)", dtype=bool, record='bo')
    FlatFieldCorrection = pvproperty(doc="do you want flat field correction applied by the detector (using int maths)", dtype=bool, record='bo')
    Initialize: bool = pvproperty(doc="Initialize the detector, resets to False immediately", dtype=bool, record='bo')
    Trigger: bool = pvproperty(doc="Trigger the detector to take an image, resets to False immediately. Adjusts detector_state to 'busy' for the duration of the measurement.", dtype=bool, record='bo')
    OutputFilePrefix = pvproperty(value="eiger_", doc="Set the prefix of the main and data output files", dtype=str, record='stringin')
    OutputFileMain = pvproperty(doc="Shows the name of the latest main output file", dtype=str, record='stringin')
    OutputFileData = pvproperty(doc="Shows the name of the latest data output file", dtype=str, record='stringin')
    SecondsRemaining = pvproperty(doc="Shows the seconds remaining for the current exposure", dtype=int, record='longin')


    @DetectorState.scan(use_scan_field=True)
    async def DetectorState(self, instance, async_lib):
        await self.DetectorState.write(self.read_detector_configuration_safely("state", "unknown"))

    @DetectorTemperature.scan(use_scan_field=True)
    async def DetectorTemperature(self, instance, async_lib):
        await self.DetectorTemperature.write(float(self.read_detector_configuration_safely("temperature", -999.0)))

    @DetectorTime.scan(use_scan_field=True)
    async def DetectorTime(self, instance, async_lib):
        await self.DetectorTime.write(self.read_detector_configuration_safely("time", "unknown"))

    @SecondsRemaining.scan(use_scan_field=True)
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
            self._starttime = datetime.now(timezone.utc)
            self.client.sendDetectorCommand("arm")
            # TODO: check if this works:
            await self.client.sendDetectorCommand("trigger")
            self.client.sendDetectorCommand("disarm")

    # do0 = pvproperty(name="do0", doc="Digital output 0, can be 0 or 1", dtype=bool, record='bi')
    # do0_RBV = pvproperty(name="do0_RBV", doc="Readback value for digital output 0", dtype=bool, record='bi')
    # @do0.putter
    # async def do0(self, instance, value: bool):
    #     self.client.write("DO", 0, value)
    # @do0.scan(period=6, use_scan_field=True)
    # async def do0(self, instance: ChannelData, async_lib: AsyncLibraryLayer):
    #     await self.do0_RBV.write(self.client.read("DO", 0))


def main(args=None):
    parser, split_args = template_arg_parser(
        default_prefix="detector_eiger:",
        desc="EPICS IOC for setting and capturing images using the Dectris Eiger detector",
    )

    if args is None:
        args = sys.argv[1:]

    parser.add_argument("--host", required=True, type=str, help="IP address of the host/device")
    parser.add_argument("--port", required=True, type=int, help="Port number of the device")

    args = parser.parse_args()

    logging.info(f"Running Dectis Eiger IOC on {args}")

    ioc_options, run_options = split_args(args)
    ioc = DEigerIOC(host=args.host, port=args.port, **ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
