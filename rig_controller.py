import socket
from enum import Enum
import re
from typing import Dict
import lovely_logger

logger = lovely_logger.logger

DEFAULT_TIMEOUT = 10  # seconds

_RPRT_pattern = re.compile(r"RPRT (?P<RPRT>.+)")
_response_record_line_pattern = re.compile(r"(?P<record>\S+): (?P<value>.+)")


class PTT(Enum):
    RX = 0
    TX = 1
    TX_MIC = 2
    TX_DATA = 3


def _parse_response(response: str) -> Dict[str, str]:
    response_lines = re.split("\n", response)
    response_lines = response_lines[1:-2]
    return {match.group("record"): match.group("value")
            for match in map(lambda line: _response_record_line_pattern.match(line), response_lines)}


def _get_RPRT(response: str) -> int:
    match = _RPRT_pattern.search(response)
    if not match:
        raise ValueError("Could not find return code for rigctld command.")
    return int(match.group("RPRT"))


class RigController:
    def __init__(self, address, port, timeout=DEFAULT_TIMEOUT, disable_ptt=False, switch_to_mem_mode=True):
        self.sct = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sct.connect((address, port))
        self.sct.settimeout(timeout)
        self.disable_ptt = disable_ptt
        if switch_to_mem_mode:
            self._switch_to_memory_mode()

    def __del__(self):
        self.sct.shutdown(socket.SHUT_RDWR)
        self.sct.close()

    def set_ptt(self, ptt: PTT):
        if not self.disable_ptt:
            self._send_command(f"\\set_ptt {ptt.value}")

    def switch_channel(self, channel: int):
        self._send_command(f"\\set_mem {channel}")

    def get_dcd_is_open(self):
        return int(self._send_command("\\get_dcd", parse_response=True)["DCD"]) == 1

    def _switch_to_memory_mode(self):
        self._send_command("\\set_vfo MEM")

    def _send_command(self, command: str, parse_response=False):
        self.sct.sendall(bytes("+" + command + "\n", "ascii"))
        response = bytearray()
        while not response.endswith(b"\n") or b"RPRT" not in response:
            data = self.sct.recv(4096)
            if not data:
                raise BrokenPipeError('Socket closed before receiving command report from rigctld.')
            response.extend(data)
        response = response.decode()
        logger.debug(response)
        RPRT = _get_RPRT(response)
        if RPRT != 0:
            raise ValueError(f"rigctld returned error code: {RPRT}")
        if parse_response:
            return _parse_response(response)
