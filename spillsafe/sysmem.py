"""Show the NVIDIA driver setting "CUDA - Sysmem Fallback Policy" (read-only, no admin needed).

  spillsafe-sysmem        (or: python -m spillsafe.sysmem)

Part of spillsafe, created by 0xydendrum. PolyForm Noncommercial License 1.0.0.

The policy decides what happens when a CUDA program asks for more VRAM than is free:
  0 / not set  Driver default: fall back to system RAM (driver 536.40+)
  1            Prefer No Sysmem Fallback: the allocation fails (out of memory)
  2            Prefer Sysmem Fallback: fall back to system RAM

It is a hidden setting: NVAPI's public name table doesn't list it, and on driver 621.14
NvAPI_DRS_SetSetting refuses it (NVAPI_SETTING_NOT_FOUND), so it can only be changed in the
NVIDIA Control Panel -> Manage 3D settings -> CUDA - Sysmem Fallback Policy. Reading it works
through NvAPI_DRS_EnumSettings, which returns hidden settings too.
Id and values come from nvidiaProfileInspector's CustomSettingNames.xml (checked November 2026).
0x10ECDB82, an id sometimes quoted for it, is actually "Antialiasing - Behavior Flags".
"""
import ctypes
import sys
from ctypes import Structure, Union, byref, c_int32, c_uint8, c_uint16, c_uint32, c_void_p

SETTING_ID = 0x10ECECC9
VALUE_NAMES = {0: "Driver Default (falls back to system RAM)", 1: "Prefer No Sysmem Fallback (OOM when VRAM is full)",
               2: "Prefer Sysmem Fallback"}
NvU16Str = c_uint16 * 2048


class BinarySetting(Structure):
    _fields_ = [("valueLength", c_uint32), ("valueData", c_uint8 * 4096)]


class SettingValue(Union):
    _fields_ = [("u32", c_uint32), ("binary", BinarySetting), ("wsz", NvU16Str)]


class NVDRS_SETTING(Structure):
    _fields_ = [("version", c_uint32), ("settingName", NvU16Str), ("settingId", c_uint32),
                ("settingType", c_uint32), ("settingLocation", c_uint32), ("isCurrentPredefined", c_uint32),
                ("isPredefinedValid", c_uint32), ("predefined", SettingValue), ("current", SettingValue)]


SETTING_VER = ctypes.sizeof(NVDRS_SETTING) | (1 << 16)
IDS = {"Initialize": 0x0150E828, "DRS_CreateSession": 0x0694D52E, "DRS_DestroySession": 0xDAD9CFF8,
       "DRS_LoadSettings": 0x375DBD6B, "DRS_GetBaseProfile": 0xDA8466A0, "DRS_EnumProfiles": 0xBC371EE0,
       "DRS_EnumSettings": 0xAE3039DA}
NVAPI_END_ENUMERATION = -7


def nvapi():
    qi = ctypes.WinDLL("nvapi64.dll").nvapi_QueryInterface
    qi.restype, qi.argtypes = c_void_p, [c_uint32]

    def fn(name, nargs):
        ptr = qi(IDS[name])
        if not ptr:
            raise RuntimeError(f"NvAPI_{name} is not available in this driver")
        return ctypes.CFUNCTYPE(c_int32, *([c_void_p] * nargs))(ptr)

    return fn


def read_policy():
    """Return (base_profile_value or None, [(profile_index, value)] for per-program overrides, profiles scanned)."""
    fn = nvapi()
    if fn("Initialize", 0)() != 0:
        raise RuntimeError("NvAPI_Initialize failed (no NVIDIA driver?)")
    sess = c_void_p()
    fn("DRS_CreateSession", 1)(byref(sess))
    try:
        if fn("DRS_LoadSettings", 1)(sess) != 0:
            raise RuntimeError("NvAPI_DRS_LoadSettings failed")
        base = c_void_p()
        fn("DRS_GetBaseProfile", 2)(sess, byref(base))
        enum_profiles, enum_settings = fn("DRS_EnumProfiles", 3), fn("DRS_EnumSettings", 5)
        n_max = 400
        arr = (NVDRS_SETTING * n_max)()
        base_value, overrides, i = None, [], 0
        while True:
            prof = c_void_p()
            st = enum_profiles(sess, c_void_p(i), byref(prof))
            if st == NVAPI_END_ENUMERATION:
                break
            if st != 0:
                raise RuntimeError(f"NvAPI_DRS_EnumProfiles failed: {st}")
            for k in range(n_max):
                arr[k].version = SETTING_VER
            n = c_uint32(n_max)
            if enum_settings(sess, prof, c_void_p(0), byref(n), arr) == 0:
                for k in range(n.value):
                    if arr[k].settingId == SETTING_ID:
                        if prof.value == base.value:
                            base_value = arr[k].current.u32
                        else:
                            overrides.append((i, arr[k].current.u32))
            i += 1
        return base_value, overrides, i
    finally:
        fn("DRS_DestroySession", 1)(sess)


def main():
    base_value, overrides, scanned = read_policy()
    shown = VALUE_NAMES[0] if base_value is None else f"{base_value} = {VALUE_NAMES.get(base_value, 'unknown')}"
    print(f"CUDA - Sysmem Fallback Policy (0x{SETTING_ID:08X})")
    print(f"  global: {'not set -> ' if base_value is None else ''}{shown}")
    print(f"  per-program overrides ({scanned} profiles scanned): "
          + (", ".join(f"profile #{p} -> {VALUE_NAMES.get(v, v)}" for p, v in overrides) or "none"))
    spill_on = base_value in (None, 0, 2)
    print(f"  => VRAM overflow {'spills into system RAM' if spill_on else 'FAILS with out-of-memory'}"
          + ("" if not overrides else " (except in the programs above)"))
    return 0 if spill_on else 1


def entry():
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    entry()
