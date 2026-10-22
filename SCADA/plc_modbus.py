"""
plc_modbus.py — Adapter Modbus untuk Modicon M241 (TM241CE24T)
================================================================
Kode lama (M221) memakai COIL Modbus (FC01 read_coils / FC05 write_coil)
untuk bit %M (trip beban %M21–%M43, status generator %M0–%M3,
perintah generator %M50–%M53).

Server Modbus TCP bawaan M241 TIDAK memetakan coil ke %M:
  FC01/FC15 -> %Q (output fisik), FC05 tidak didukung,
  FC03/FC06/FC16 -> %MW (holding register).

Project hasil konversi Machine Expert menaruh setiap bit M221 %Mn di
  %MX(20000 + n//8).(n%8)  ==  %MW(10000 + n//16), bit (n % 16)
sehingga adapter ini membaca/menulis "coil n" sebagai bit di holding
register tersebut. load.py dan app.py tidak perlu diubah selain baris import.

Variabel lingkungan:
  PLC_UNIT_ID       Unit ID server Modbus M241 (default 255)
  PLC_COIL_BASE_MW  %MW awal area bit %M (default 10000)
"""
import os
from pymodbus.client import ModbusTcpClient as _BaseClient

UNIT_ID = int(os.getenv("PLC_UNIT_ID", 255))
COIL_BASE_MW = int(os.getenv("PLC_COIL_BASE_MW", 10000))


class _BitsResponse:
    """Meniru objek respons pymodbus untuk read_coils (punya .bits & .isError())."""

    def __init__(self, bits):
        self.bits = bits

    def isError(self):  # noqa: N802 (nama mengikuti pymodbus)
        return False


def _word_bit(n):
    return COIL_BASE_MW + n // 16, n % 16


class M241Client(_BaseClient):
    """ModbusTcpClient dengan emulasi coil %M di atas %MW (untuk M241)."""

    # ---- coil (bit %M) -------------------------------------------------
    def read_coils(self, address, count=1, **kwargs):
        first_w, _ = _word_bit(address)
        last_w, _ = _word_bit(address + count - 1)
        rr = super().read_holding_registers(
            address=first_w, count=last_w - first_w + 1, slave=UNIT_ID)
        if rr.isError():
            return rr
        bits = []
        for n in range(address, address + count):
            w, b = _word_bit(n)
            bits.append(bool((rr.registers[w - first_w] >> b) & 1))
        return _BitsResponse(bits)

    def write_coil(self, address, value, **kwargs):
        # read-modify-write: setiap word hanya ditulis oleh satu program
        # (load.py -> %MW10000, app.py -> %MW10001..10003), jadi aman.
        w, b = _word_bit(address)
        rr = super().read_holding_registers(address=w, count=1, slave=UNIT_ID)
        if rr.isError():
            return rr
        cur = rr.registers[0]
        new = (cur | (1 << b)) if value else (cur & ~(1 << b) & 0xFFFF)
        if new == cur:
            return rr
        return super().write_register(address=w, value=new, slave=UNIT_ID)

    # ---- holding register (%MW): sama seperti M221, hanya Unit ID ------
    def read_holding_registers(self, address, count=1, **kwargs):
        kwargs.setdefault("slave", UNIT_ID)
        return super().read_holding_registers(address=address, count=count, **kwargs)

    def write_register(self, address, value, **kwargs):
        kwargs.setdefault("slave", UNIT_ID)
        return super().write_register(address=address, value=value, **kwargs)

    def write_registers(self, address, values, **kwargs):
        kwargs.setdefault("slave", UNIT_ID)
        return super().write_registers(address=address, values=values, **kwargs)
