"""
plc_modbus.py - Adapter Modbus untuk Modicon M241 (TM241CE24T)
================================================================
Kode lama (M221) memakai COIL Modbus (FC01/FC05) untuk bit %M.
Server Modbus TCP M241 tidak memetakan coil ke %M (FC01/FC15 -> %Q,
FC05 tidak didukung); yang tersedia adalah holding register %MW
(FC03/FC06/FC16).

Project hasil konversi Machine Expert menaruh bit M221 %Mn di
    %MX(20000 + n//8).(n%8)  ==  %MW(10000 + n//16), bit (n % 16)
sehingga adapter ini membaca/menulis "coil n" sebagai bit di holding
register tersebut.

Variabel lingkungan:
    PLC_UNIT_ID       Unit ID server Modbus M241 (default 255)
    PLC_COIL_BASE_MW  %MW awal area bit %M (default 10000)
"""
import os
from pymodbus.client import ModbusTcpClient as _BaseClient
from pymodbus.exceptions import ConnectionException, ModbusIOException

UNIT_ID = int(os.getenv("PLC_UNIT_ID", 255))
COIL_BASE_MW = int(os.getenv("PLC_COIL_BASE_MW", 10000))


class _BitsResponse:
    """Meniru respons pymodbus read_coils (punya .bits dan .isError())."""

    def __init__(self, bits):
        self.bits = bits

    def isError(self):  # noqa: N802 (nama mengikuti pymodbus)
        return False


def _word_bit(n):
    return COIL_BASE_MW + n // 16, n % 16


class M241Client(_BaseClient):
    """ModbusTcpClient dengan emulasi coil %M di atas %MW (untuk M241)."""

    # ---- koneksi ---------------------------------------------------------
    def execute(self, no_response_expected, request):
        """Transaksi gagal (PLC restart, Wi-Fi putus, timeout) -> socket ditutup.
        Tanpa ini connect() pymodbus terus mengembalikan True untuk socket yang
        sudah mati, sehingga klien tidak pernah tersambung ulang."""
        try:
            return super().execute(no_response_expected, request)
        except (ConnectionException, ModbusIOException, OSError):
            self.close()
            raise

    # ---- bit %M ("coil") ------------------------------------------------
    def read_coils(self, address, count=1, **kwargs):
        """Baca `count` bit mulai %M`address` dalam SATU transaksi FC03."""
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
        """Tulis satu bit %M (read-modify-write satu word)."""
        return self.write_coil_map({address: value})

    def write_coil_map(self, bit_values):
        """Tulis banyak bit %M sekaligus: {nomor_bit: True/False}.

        Satu FC03 untuk membaca semua word yang terlibat, lalu satu FC16
        hanya jika ada word yang berubah. Tidak ada tulis jika semua bit
        sudah sesuai.
        """
        if not bit_values:
            return None
        words = {}
        for n, value in bit_values.items():
            w, b = _word_bit(n)
            words.setdefault(w, []).append((b, bool(value)))
        first_w, last_w = min(words), max(words)
        rr = super().read_holding_registers(
            address=first_w, count=last_w - first_w + 1, slave=UNIT_ID)
        if rr.isError():
            return rr
        new_regs = list(rr.registers)
        for w, changes in words.items():
            reg = new_regs[w - first_w]
            for b, value in changes:
                reg = (reg | (1 << b)) if value else (reg & ~(1 << b) & 0xFFFF)
            new_regs[w - first_w] = reg
        changed = [i for i, (old, new) in enumerate(zip(rr.registers, new_regs)) if old != new]
        if not changed:
            return rr
        lo, hi = changed[0], changed[-1]
        return super().write_registers(
            address=first_w + lo, values=new_regs[lo:hi + 1], slave=UNIT_ID)

    # ---- holding register %MW: sama seperti M221, hanya Unit ID ---------
    def read_holding_registers(self, address, count=1, **kwargs):
        kwargs.setdefault("slave", UNIT_ID)
        return super().read_holding_registers(address=address, count=count, **kwargs)

    def write_register(self, address, value, **kwargs):
        kwargs.setdefault("slave", UNIT_ID)
        return super().write_register(address=address, value=value, **kwargs)

    def write_registers(self, address, values, **kwargs):
        kwargs.setdefault("slave", UNIT_ID)
        return super().write_registers(address=address, values=values, **kwargs)

    def readwrite_registers(self, read_address, read_count, write_address, values, **kwargs):
        """FC23: tulis `values` mulai write_address lalu baca read_count register mulai
        read_address, dalam SATU transaksi (didukung server Modbus M241)."""
        kwargs.setdefault("slave", UNIT_ID)
        return super().readwrite_registers(read_address=read_address, read_count=read_count,
                                           write_address=write_address, values=values, **kwargs)
