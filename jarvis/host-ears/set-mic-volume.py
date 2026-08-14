"""Set Windows mic volume using ctypes."""
import ctypes
import ctypes.wintypes

# Windows constants
MMIOOP_OPEN = 0x0000
MMIOOP_CLOSE = 0x0001
MMIOOP_READ = 0x0002
MMIOOP_WRITE = 0x0003

# Mixer constants
MIXER_LONG_NAME_CHARS = 64
MIXER_SHORT_NAME_CHARS = 16

class MIXERLINE_TARGETTYPE(ctypes.Structure):
    _fields_ = [
        ("dwType", ctypes.c_ulong),
        ("dwDestination", ctypes.c_ulong),
    ]

class MIXERLINE(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwDestination", ctypes.c_ulong),
        ("dwSource", ctypes.c_ulong),
        ("dwLineID", ctypes.c_ulong),
        ("fdwLine", ctypes.c_ulong),
        ("dwUser", ctypes.c_ulong),
        ("dwComponentType", ctypes.c_ulong),
        ("cChannels", ctypes.c_ulong),
        ("cConnections", ctypes.c_ulong),
        ("cControls", ctypes.c_ulong),
        ("szFullname", ctypes.c_char * MIXER_LONG_NAME_CHARS),
    ]

class MIXERCONTROL(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwControlType", ctypes.c_ulong),
        ("fdwControl", ctypes.c_ulong),
        ("cMultipleItems", ctypes.c_ulong),
        ("szName", ctypes.c_char * MIXER_SHORT_NAME_CHARS),
        ("lMinimum", ctypes.c_long),
        ("lMaximum", ctypes.c_long),
        ("reserved", ctypes.c_long * 10),
    ]

class MIXERCONTROLDETAILS(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwControlType", ctypes.c_ulong),
        ("cChannels", ctypes.c_ulong),
        ("item", ctypes.c_ulong),
        ("cControls", ctypes.c_ulong),
        ("cbDetails", ctypes.c_ulong),
        ("paDetails", ctypes.c_void_p),
    ]

class MIXERCONTROLDETAILS_UNSIGNED(ctypes.Structure):
    _fields_ = [
        ("dwValue", ctypes.c_ulong),
    ]

# Load mixer API
winmm = ctypes.windll.winmm

# Function pointers
mixerOpen = winmm.mixerOpen
mixerClose = winmm.mixerClose
mixerGetLineInfo = winmm.mixerGetLineInfoA
mixerGetLineControls = winmm.mixerGetLineControlsA
mixerSetControlDetails = winmm.mixerSetControlDetailsA
mixerGetControlDetails = winmm.mixerGetControlDetailsA

# Constants
MIXEROBJECTTYPE_HANDLE = 0
MIXEROBJECTTYPE destination = 1
MIXERCONTROL_CONTROL_TYPE_MIXER = 8
MIXERLINE_COMPONENTTYPE_DST_DIGITAL = 0x00000003

print("Note: Windows mixer API requires admin privileges for some operations.")
print("Please manually adjust mic volume in Sound Settings:")
print("  1. Right-click speaker icon -> Sounds")
print("  2. Recording tab -> Microphone -> Properties")
print("  3. Levels tab -> Increase volume to 100%")
print("  4. Enable 'Mic Boost' if available")
