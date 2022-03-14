import datetime
import struct
import typing
from collections import namedtuple
from functools import lru_cache

from cached_property import cached_property

from rpcclient.client import Client
from rpcclient.darwin import objective_c_class
from rpcclient.darwin.consts import kCFNumberSInt64Type, kCFNumberDoubleType, CFStringEncoding, kCFAllocatorDefault
from rpcclient.darwin.darwin_lief import DarwinLief
from rpcclient.darwin.fs import DarwinFs
from rpcclient.darwin.hid import Hid
from rpcclient.darwin.ioregistry import IORegistry
from rpcclient.darwin.location import Location
from rpcclient.darwin.media import DarwinMedia
from rpcclient.darwin.network import DarwinNetwork
from rpcclient.darwin.objective_c_symbol import ObjectiveCSymbol
from rpcclient.darwin.preferences import Preferences
from rpcclient.darwin.processes import DarwinProcesses
from rpcclient.darwin.structs import utsname
from rpcclient.darwin.symbol import DarwinSymbol
from rpcclient.darwin.syslog import Syslog
from rpcclient.darwin.time import Time
from rpcclient.darwin.xpc import Xpc
from rpcclient.exceptions import RpcClientException, MissingLibraryError
from rpcclient.macos.bluetooth import Bluetooth
from rpcclient.structs.consts import RTLD_NOW

IsaMagic = namedtuple('IsaMagic', 'mask value')
ISA_MAGICS = [
    # ARM64
    IsaMagic(mask=0x000003f000000001, value=0x000001a000000001),
    # X86_64
    IsaMagic(mask=0x001f800000000001, value=0x001d800000000001),
]
# Mask for tagged pointer, from objc-internal.h
OBJC_TAG_MASK = (1 << 63)


class DarwinClient(Client):

    def __init__(self, sock, sysname: str, hostname: str, port: int = None):
        super().__init__(sock, sysname, hostname, port)
        self._dlsym_global_handle = -2  # RTLD_GLOBAL

        if 0 == self.dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", RTLD_NOW):
            raise MissingLibraryError('failed to load CoreFoundation')

        self._cf_types = {
            self.symbols.CFNullGetTypeID(): 'null',
            self.symbols.CFDateGetTypeID(): 'date',
            self.symbols.CFDataGetTypeID(): 'data',
            self.symbols.CFStringGetTypeID(): 'str',
            self.symbols.CFArrayGetTypeID(): 'array',
            self.symbols.CFBooleanGetTypeID(): 'bool',
            self.symbols.CFNumberGetTypeID(): 'number',
            self.symbols.CFSetGetTypeID(): 'set',
            self.symbols.CFDictionaryGetTypeID(): 'dict',
        }

        if self.uname.machine != 'x86_64':
            self.inode64 = True
        self.fs = DarwinFs(self)
        self.preferences = Preferences(self)
        self.processes = DarwinProcesses(self)
        self.media = DarwinMedia(self)
        self.network = DarwinNetwork(self)
        self.ioregistry = IORegistry(self)
        self.location = Location(self)
        self.xpc = Xpc(self)
        self.syslog = Syslog(self)
        self.time = Time(self)
        self.hid = Hid(self)
        self.lief = DarwinLief(self)
        self.bluetooth = Bluetooth(self)

    @property
    def modules(self) -> typing.List[str]:
        m = []
        for i in range(self.symbols._dyld_image_count()):
            m.append(self.symbols._dyld_get_image_name(i).peek_str())
        return m

    @cached_property
    def uname(self):
        with self.safe_calloc(utsname.sizeof()) as uname:
            assert 0 == self.symbols.uname(uname)
            return utsname.parse_stream(uname)

    @cached_property
    def is_idevice(self):
        return self.uname.machine.startswith('i')

    @property
    def roots(self) -> typing.List[str]:
        """ get a list of all accessible darwin roots when used for lookup of files/preferences/... """
        return ['/', '/var/root']

    def symbol(self, symbol: int):
        """ at a symbol object from a given address """
        return DarwinSymbol.create(symbol, self)

    def _cf_encode_none(self, o: object) -> DarwinSymbol:
        return self.symbols.kCFNull[0]

    def _cf_encode_darwin_symbol(self, o: object) -> DarwinSymbol:
        # assuming it's already a cfobject
        return o

    def _cf_encode_str(self, o: object) -> DarwinSymbol:
        return self.symbols.CFStringCreateWithCString(kCFAllocatorDefault, o,
                                                      CFStringEncoding.kCFStringEncodingMacRoman)

    def _cf_encode_bytes(self, o: object) -> DarwinSymbol:
        return self.symbols.CFDataCreate(kCFAllocatorDefault, o, len(o))

    def _ns_encode_datetime(self, o: object) -> DarwinSymbol:
        comps = self.symbols.objc_getClass('NSDateComponents').objc_call('new')
        comps.objc_call('setDay:', o.day)
        comps.objc_call('setMonth:', o.month)
        comps.objc_call('setYear:', o.year)
        comps.objc_call('setHour:', o.hour)
        comps.objc_call('setMinute:', o.minute)
        comps.objc_call('setSecond:', o.second)
        comps.objc_call('setTimeZone:',
                        self.symbols.objc_getClass('NSTimeZone').objc_call('timeZoneWithAbbreviation:',
                                                                           self.cf('UTC')))
        return self.symbols.objc_getClass('NSCalendar').objc_call('currentCalendar') \
            .objc_call('dateFromComponents:', comps)

    def _cf_encode_bool(self, o: object) -> DarwinSymbol:
        if o:
            return self.symbols.kCFBooleanTrue[0]
        else:
            return self.symbols.kCFBooleanFalse[0]

    def _cf_encode_int(self, o: object) -> DarwinSymbol:
        with self.safe_malloc(8) as buf:
            buf[0] = o
            return self.symbols.CFNumberCreate(kCFAllocatorDefault, kCFNumberSInt64Type, buf)

    def _cf_encode_float(self, o: object) -> DarwinSymbol:
        with self.safe_malloc(8) as buf:
            buf.poke(struct.pack('<d', o))
            return self.symbols.CFNumberCreate(kCFAllocatorDefault, kCFNumberDoubleType, buf)

    def _cf_encode_list(self, o: object) -> DarwinSymbol:
        cfvalues = [self.cf(i) for i in o]
        with self.safe_malloc(8 * len(cfvalues)) as buf:
            for i in range(len(cfvalues)):
                buf[i] = cfvalues[i]
            return self.symbols.CFArrayCreate(kCFAllocatorDefault, buf, len(cfvalues), 0)

    def _cf_encode_dict(self, o: object) -> DarwinSymbol:
        cfkeys = [self.cf(i) for i in o.keys()]
        cfvalues = [self.cf(i) for i in o.values()]
        with self.safe_malloc(8 * len(cfkeys)) as keys_buf:
            with self.safe_malloc(8 * len(cfvalues)) as values_buf:
                for i in range(len(cfkeys)):
                    keys_buf[i] = cfkeys[i]
                for i in range(len(cfvalues)):
                    values_buf[i] = cfvalues[i]
                return self.symbols.CFDictionaryCreate(
                    kCFAllocatorDefault, keys_buf, values_buf, len(cfvalues), 0, 0, 0)

    def cf(self, o: object) -> DarwinSymbol:
        """ construct a CFObject from a given python object """
        if o is None:
            return self._cf_encode_none(o)

        encoders = {
            DarwinSymbol: self._cf_encode_darwin_symbol,
            str: self._cf_encode_str,
            bytes: self._cf_encode_bytes,
            datetime.datetime: self._ns_encode_datetime,
            bool: self._cf_encode_bool,
            int: self._cf_encode_int,
            float: self._cf_encode_float,
            list: self._cf_encode_list,
            tuple: self._cf_encode_list,
            dict: self._cf_encode_dict,
        }

        for type_, encoder in encoders.items():
            if isinstance(o, type_):
                return encoder(o)

        raise NotImplementedError()

    def objc_symbol(self, address) -> ObjectiveCSymbol:
        """
        Get objc symbol wrapper for given address
        :param address:
        :return: ObjectiveC symbol object
        """
        return ObjectiveCSymbol.create(int(address), self)

    @lru_cache(maxsize=None)
    def objc_get_class(self, name: str):
        """
        Get ObjC class object
        :param name:
        :return:
        """
        return objective_c_class.Class.from_class_name(self, name)

    @staticmethod
    def is_objc_type(symbol: DarwinSymbol) -> bool:
        """
        Test if a given symbol represents an objc object
        :param symbol:
        :return:
        """
        # Tagged pointers are ObjC objects
        if symbol & OBJC_TAG_MASK == OBJC_TAG_MASK:
            return True

        # Class are not ObjC objects
        for mask, value in ISA_MAGICS:
            if symbol & mask == value:
                return False

        try:
            with symbol.change_item_size(8):
                isa = symbol[0]
        except RpcClientException:
            return False

        for mask, value in ISA_MAGICS:
            if isa & mask == value:
                return True

        return False
