import enum
import os
import re


DEBUG_MAX_SIZE = 30
FIRST_INSTALL_SENTINEL = '/data/first-boot'
RE_KDUMP_CONFIGURED = re.compile(r'current state\s*:\s*(ready to kdump)', flags=re.M)


class VMProvider(enum.Enum):
    AZURE = 'AZURE'
    NONE = 'NONE'


class Lifecycle:
    def __init__(self):
        self.SYSTEM_BOOT_ID = None
        self.SYSTEM_FIRST_BOOT = False
        # Flag telling whether the system completed boot and is ready to use
        self.SYSTEM_READY = False
        # Flag telling whether the system is shutting down
        self.SYSTEM_SHUTTING_DOWN = False


def get_debug_execution_dir(system_dataset_path: str) -> str:
    return '/var/tmp/ixdiagnose' if system_dataset_path is None else os.path.join(system_dataset_path, 'ixdiagnose')


lifecycle_conf = Lifecycle()
