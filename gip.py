import argparse
import enum
import sys
import time

import usb.core
import usb.util


class Seq:
    def __init__(self) -> None:
        self._value = 0

    @property
    def value(self) -> int:
        r = self._value
        self._value = (self._value + 1) & 0xff
        return r

    @property
    def current_value(self) -> int:
        return self._value

    def __int__(self) -> int:
        return self.value


class GipCmd(enum.IntEnum):
    Identify = 0x04
    Power = 0x05
    Authenticate = 0x06
    Rumble = 0x09
    LED = 0x0a


SEQ_INDEX = 2


def make_gip_packet(cmd: GipCmd, *args: int, seq: Seq):
    data = [
        cmd.value,
        0x20,  # internal
        int(seq),  # seq
        -1,  # len
        *args,
    ]
    data[3] = len(data) - 4
    return data


GIP_MOTOR_R = 0x1
GIP_MOTOR_L = 0x2
GIP_MOTOR_RT = 0x4
GIP_MOTOR_LT = 0x8
GIP_MOTOR_ALL = 0xf


def make_gip_rumble_packet(lt, rt, la, ra, seq: Seq, on=0xff, off=0x00, repeat=0xff):
    data = [
        GipCmd.Rumble.value,
        0x00,
        int(seq),
        -1,
        0x00,
        GIP_MOTOR_ALL,
        lt,
        rt,
        la,
        ra,
        on,
        off,
        repeat,
    ]
    data[3] = len(data) - 4
    return data


def to_signed_16(n: int) -> int:
    n = n & 0xffff
    return n | (-(n & 0x8000))


def le16(value: list[int]) -> int:
    return to_signed_16(value[0] | (value[1] << 8))


def get_bit(array: list[int], byte: int, bit: int) -> bool:
    if byte > len(array):
        return False
    return (array[byte] & (1 << bit)) > 0


def active(text: str) -> str:
    return f"\033[91m{text}\033[0m"


def add_if(to: list[str], name: str, cond: bool):
    if cond:
        to.append(active(name))
    else:
        to.append(name.lower())


def add_val(to: list[str], fmt: str, value: int):
    to.append(fmt % value)
    return value


def parse_input(data: list[int]) -> tuple[list[str], list[str], tuple[int, ...] | None]:
    if data[0] != 0x20:
        # Not INPUT packet
        return [], [], None

    state = []
    add_if(state, "SELECT", get_bit(data, 4, 3))
    add_if(state, "START", get_bit(data, 4, 2))
    add_if(state, "REC", get_bit(data, 22, 0))

    add_if(state, "A", get_bit(data, 4, 4))
    add_if(state, "B", get_bit(data, 4, 5))
    add_if(state, "X", get_bit(data, 4, 6))
    add_if(state, "Y", get_bit(data, 4, 7))

    add_if(state, "L", get_bit(data, 5, 2))
    add_if(state, "R", get_bit(data, 5, 3))
    add_if(state, "U", get_bit(data, 5, 0))
    add_if(state, "D", get_bit(data, 5, 1))

    # "Bumper"
    add_if(state, "LB", get_bit(data, 5, 4))
    add_if(state, "RB", get_bit(data, 5, 5))

    # Stick press
    add_if(state, "SL", get_bit(data, 5, 6))
    add_if(state, "SR", get_bit(data, 5, 7))

    numbers = []
    add_val(numbers, "PRO:%d", data[34])

    lt = add_val(numbers, "LT:%+05x", le16(data[6:]))
    rt = add_val(numbers, "RT:%+05x", le16(data[8:]))

    lx = add_val(numbers, "LX:%+05x", le16(data[10:]))
    ly = add_val(numbers, "LY:%+05x", le16(data[12:]))
    rx = add_val(numbers, "RX:%+05x", le16(data[14:]))
    ry = add_val(numbers, "RY:%+05x", le16(data[16:]))
    return state, numbers, (lx, ly, rx, ry, lt, rt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("vendor", help="Vendor id in HEX")
    parser.add_argument("product", help="Product id in HEX")
    args = parser.parse_args()

    vendor = int(args.vendor.removeprefix("0x"), 16)
    product = int(args.product.removeprefix("0x"), 16)

    not_found_informed = False
    while (dev := usb.core.find(idVendor=vendor, idProduct=product)) is None:
        if not not_found_informed:
            print("Plug a (specific) GIP game pad into USB port...")
        not_found_informed = True
        time.sleep(0.1)

    dev.set_configuration()
    cfg = dev.get_active_configuration()
    ifce: usb.core.Interface = cfg[(0, 0)]

    print("Timeout:", dev.default_timeout)

    epi, epo = None, None
    for ep in ifce:
        ep: usb.core.Endpoint
        match usb.util.endpoint_direction(ep.bEndpointAddress):
            case usb.util.ENDPOINT_IN:
                epi = ep
            case usb.util.ENDPOINT_OUT:
                epo = ep
            case a:
                print("Unknown endpoint direction", a, ep)

    if epi is None or epo is None:
        print("Endpoint config not resolved", ifce)
        return
    print("->", epo.bEndpointAddress, "-<", epi.bEndpointAddress)
    print(epo, epi, sep="\n")

    seq = Seq()

    epo.write(make_gip_packet(GipCmd.Power, 0x00, seq=seq))
    epo.write(make_gip_packet(GipCmd.LED, 0x00, 0x01, 0x14, seq=seq))

    print("\n\nTo exit, long press SELECT or START ")

    # store_buttons = []
    store_system = "sys"
    # store_numbers = []
    store_rumble = []
    exit_start = -1

    # Initial read should probably be done somehow with a request instead of irq-wait.
    store_buttons, store_numbers, _ = parse_input([0x20] + [0] * 40)
    print("", " ".join(store_buttons), store_system, " ".join(store_numbers), "seq:%3d" % seq.current_value, end="")
    sys.stdout.flush()

    while exit_start < 0 or time.time() - exit_start < 2.0:
        try:
            data = epi.read(epi.wMaxPacketSize)
        except usb.core.USBTimeoutError:
            continue

        if data[0] == 0x03:
            # IDK?
            continue

        elif data[0] == 0x07:
            # SYSTEM
            if data[4] == 1:
                store_system = active("SYS")
            else:
                store_system = "sys"

        elif data[0] == 0x20:
            # INPUT
            inp, numbers, values = parse_input(data)
            store_buttons = inp
            store_numbers = numbers

            # Rumble test code
            rumble = [min(abs(v // 128), 255) for v in values[0:4]]
            on = 255 - min(values[4] // 4, 255)
            off = min(values[5] // 4, 255)
            new_rumble = [*rumble, on, off]
            if new_rumble != store_rumble:
                epo.write(make_gip_rumble_packet(*rumble, on=on, off=off, seq=seq))
                store_rumble = new_rumble

            # Exit logic
            if exit_start < 0 and len(data) >= 5 and (data[4] & 0x04 or data[4] & 0x08):
                exit_start = time.time()
            else:
                exit_start = -1
        else:
            print(" ".join(f"{c:02x}" for c in data))

        print("\r", " ".join(store_buttons), store_system, " ".join(store_numbers), "seq:%3d" % seq.current_value,
              end="")
        sys.stdout.flush()

    epo.write(make_gip_rumble_packet(0, 0, 0, 0, seq=seq))
    print("\nDone")


if __name__ == "__main__":
    main()
