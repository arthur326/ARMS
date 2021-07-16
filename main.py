import threading
from types import SimpleNamespace

import lovely_logger as logger
import time
import toml
from enum import Enum, auto
from itertools import cycle
from pathlib import Path
from time import sleep
from typing import Union
from numbers import Real

import player
from dtmf_reader import read_dtmf, get_next_dtmf, Tone
from rig_controller import RigController, PTT


class ARMS:
    def __init__(self, cfg):
        self._cfg = cfg
        self._rigctlr = RigController(self._cfg.RIGCTLD_ADDRESS, self._cfg.RIGCTLD_PORT
                                      , self._cfg.RIGCTLD_OPERATION_TIMEOUT, disable_ptt=self._cfg.DISABLE_PTT
                                      , switch_to_mem_mode=self._cfg.SWITCH_TO_MEM_MODE)

    def begin_operation(self):
        self._rigctlr.set_ptt(PTT.RX)
        self._init_audio_io()
        self._load_audio_files()
        self._set_not_in_alert_flag(True)

        while True:
            for ch in range(6, self._cfg.LAST_CHANNEL + 1):
                self._rigctlr.switch_channel(ch)
                if self._detect_tone(Tone.ZERO) == Tone.ZERO:
                    self._set_not_in_alert_flag(False)
                    if self._detect_lpz():
                        self._alert_procedure(ch)
                    self._set_not_in_alert_flag(True)

    def _set_not_in_alert_flag(self, not_in_alert: bool):
        try:
            if not_in_alert:
                self._cfg.NOT_IN_ALERT_FLAG_PATH.touch(exist_ok=True)
            else:
                self._cfg.NOT_IN_ALERT_FLAG_PATH.unlink()  # missing_ok argument requires python 3.8.
        except Exception:
            logger.exception("Error " + ("creating" if not_in_alert else "removing") + " not_in_alert flag file."
                                                                                       " Continuing operation.")

    def _alert_procedure(self, ch: int):
        logger.info(f"Entering alert procedure; channel: {ch}.")
        self._transmit_files(self._cfg.SYS_CALLING_HELP_PATH, call_sign=True)
        logger.info("Listening for cancellation via DTMF 3.")
        if self._wait_for_silence_and_tone(self._cfg.CANCEL_HELP_TIMEOUT, Tone.THREE) == Tone.THREE:
            logger.info("Tone 3 detected. Cancelling alert procedure.")
            self._transmit_files(self._cfg.ALERT_CANCELLED_PATH, self._repeater_name_path(ch), call_sign=True)
            return

        self._rigctlr.switch_channel(1)
        delays = [self._cfg.MESSAGE_LOOP_SHORT_DELAY] * self._cfg.NUM_SHORT_DELAYS + [self._cfg.MESSAGE_LOOP_LONG_DELAY]
        delay_index = 0

        class State(Enum):
            PLAYING_INFO = auto()
            WAITING = auto()
        states_iter = cycle(State)

        logger.info("Playing first message on channel 1.")
        self._transmit_files(self._cfg.LPZ_DETECTED_PATH, self._repeater_name_path(ch)
                             , beep=self.Beep.ASCENDING, call_sign=True)
        next(states_iter)

        while True:
            state = next(states_iter)
            if state == State.PLAYING_INFO:
                logger.info("Playing message on channel 1.")
                self._transmit_files(self._cfg.LPZ_DETECTED_PATH, self._repeater_name_path(ch))
            elif state == State.WAITING:
                logger.info("Awaiting command on channel 1.")
                tone = self._wait_for_tone(delays[delay_index], Tone.THREE, Tone.FIVE)
                if tone == Tone.THREE:
                    logger.info("Tone 3 detected. Cancelling alert procedure.")
                    self._transmit_files(self._cfg.ALERT_CANCELLED_PATH, self._repeater_name_path(ch), call_sign=True)
                    return
                elif tone == Tone.FIVE:
                    logger.info("Tone 5 detected. Repeating message on channel 1.")
                    states_iter = cycle(State)
                    delay_index = 0
                    continue
                delay_index = (delay_index + 1) % len(delays)

    class Beep(Enum):
        NO_BEEP = auto()
        ORDINARY = auto()
        ASCENDING = auto()

    def _transmit_files(self, *filepaths, beep: Beep = Beep.ORDINARY, call_sign: bool = False):
        logger.info(f"Waiting for silence. "
                    f"({self._cfg.DCD_REQ_CONSEC_ZEROES} consecutive zeroes,"
                    f" {self._cfg.DCD_SAMPLING_PERIOD} ms sampling period.)")
        consec_dcd_0_count = 0
        while True:
            if self._rigctlr.get_dcd_is_open():
                consec_dcd_0_count = 0
            else:
                consec_dcd_0_count += 1
            if consec_dcd_0_count >= self._cfg.DCD_REQ_CONSEC_ZEROES:
                break
            self._sleep_millis(self._cfg.DCD_SAMPLING_PERIOD)
        logger.info("Transmitting audio.")
        self._rigctlr.set_ptt(PTT.TX)
        sleep(self._cfg.TRANSMIT_DELAY)
        if beep == self.Beep.ORDINARY:
            player.play(self._cfg.BEEP_PATH)
        elif beep == self.Beep.ASCENDING:
            player.play(self._cfg.ASCENDING_BEEP_PATH)
        for path in filepaths:
            player.play(path)
        if call_sign:
            player.play(self._cfg.CALL_SIGN_PATH)
        self._rigctlr.set_ptt(PTT.RX)

    def _wait_for_silence_and_tone(self, timeout_seconds, *tones) -> Union[Tone, None]:
        status = SimpleNamespace()
        status.cond = threading.Condition()
        status.awaiting_silence = True
        status.deadline = None

        def target():
            consec_dcd_0_count = 0
            while status.awaiting_silence:
                if self._rigctlr.get_dcd_is_open():
                    consec_dcd_0_count = 0
                else:
                    consec_dcd_0_count += 1
                if consec_dcd_0_count >= self._cfg.DCD_REQ_CONSEC_ZEROES:
                    break
                with status.cond:
                    if status.cond.wait_for(lambda: not status.awaiting_silence, timeout=self._cfg.DCD_SAMPLING_PERIOD / 1000):
                        return
            with status.cond:
                if status.awaiting_silence:
                    status.awaiting_silence = False
                    status.deadline = time.time() + timeout_seconds
        silence_waiting_thread = threading.Thread(target=target)
        silence_waiting_thread.start()
        while True:
            if status.awaiting_silence:
                tone = self._wait_for_tone(timeout_seconds, *tones)
                if tone is not None:
                    with status.cond:
                        status.awaiting_silence = False
                        status.cond.notify()
                    logger.info("Tone detected before final timeout was started.")
                    return tone
            else:
                logger.info("Silence criteria reached. Starting final timeout.")
                with status.cond:
                    timeout = status.deadline - time.time()
                silence_waiting_thread.join()
                return self._wait_for_tone(max(timeout, 0), *tones)

    def _repeater_name_path(self, ch: int):
        return self._cfg.REPEATER_NAME_DIRECTORY / "{:02d}.wav".format(ch)

    def _load_audio_files(self):
        player.load(self._cfg.SYS_CALLING_HELP_PATH)
        player.load(self._cfg.LPZ_DETECTED_PATH)
        player.load(self._cfg.ALERT_CANCELLED_PATH)
        player.load(self._cfg.BEEP_PATH)
        player.load(self._cfg.ASCENDING_BEEP_PATH)
        player.load(self._cfg.CALL_SIGN_PATH)
        for ch in range(6, self._cfg.LAST_CHANNEL + 1):
            player.load(self._repeater_name_path(ch))

    def _init_audio_io(self):
        for i in range(4):
            try:
                player.set_default_io(None, self._cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING)
            except Exception:
                if i < 3:
                    sleep(3)
                else:
                    raise

    def _sleep_millis(self, millis: float):
        sleep(millis / 1000)

    def _detect_lpz(self) -> bool:
        zero_count = 0
        start_time = time.time()
        for i in range(self._cfg.LPZ_TOTAL_SAMPLES):
            sleep_ms = i * self._cfg.LPZ_SAMPLING_PERIOD - 1000 * (time.time() - start_time)
            if sleep_ms > 0:
                self._sleep_millis(sleep_ms)
            if read_dtmf() == Tone.ZERO:
                zero_count += 1
        return zero_count >= self._cfg.LPZ_REQUIRED_ZERO_COUNT and zero_count <= self._cfg.LPZ_MAX_ZERO_COUNT

    def _wait_for_tone(self, timeout_seconds, *tones) -> Union[Tone, None]:
        time_rem = timeout_seconds
        while time_rem > 0:
            start_time = time.time()
            tone = get_next_dtmf(timeout_ms=max(int((time_rem * 1000)), 1))
            if tone in tones:
                return tone
            time_rem -= (time.time() - start_time)
        return None

    def _detect_tone(self, *tones) -> bool:
        """
        Used to detect tones while scanning. Uses _wait_for_tone with TONE_DETECT_TIMEOUT but also checks for the
        presence of a tone at the end on the off-chance that a tone appeared before waiting began.
        """
        tone = self._wait_for_tone(self._cfg.TONE_DETECT_TIMEOUT / 1000, *tones)
        if tone not in tones:
            tone = read_dtmf()
        return tone if tone in tones else None


def parse_cfg(cfg_path):
    cfg_dict = toml.load(cfg_path)
    cfg = SimpleNamespace()

    def verify_field(value, predicate, exception_msg):
        if not predicate(value):
            raise TypeError(exception_msg)

    cfg.LAST_CHANNEL = cfg_dict['LAST_CHANNEL']
    cfg.TONE_DETECT_TIMEOUT = cfg_dict.get('TONE_DETECT_TIMEOUT')  # ms, initial detection of 0 timeout.
    cfg.MESSAGE_LOOP_SHORT_DELAY = cfg_dict.get('MESSAGE_LOOP_SHORT_DELAY', 5)  # seconds
    cfg.NUM_SHORT_DELAYS = cfg_dict.get('NUM_SHORT_DELAYS', 2)
    cfg.MESSAGE_LOOP_LONG_DELAY = cfg_dict.get('MESSAGE_LOOP_LONG_DELAY', 300)  # seconds
    cfg.CANCEL_HELP_TIMEOUT = cfg_dict.get('CANCEL_HELP_TIMEOUT')
    cfg.TRANSMIT_DELAY = cfg_dict.get('TRANSMIT_DELAY',
                                        1.5)  # seconds. Delay after activating PTT and before playing files.
    verify_field(cfg.LAST_CHANNEL, lambda ch: isinstance(ch, int) and ch >= 6
                 , "LAST_CHANNEL must be an integer greater than or equal to 6.")
    verify_field(cfg.TONE_DETECT_TIMEOUT, lambda t: isinstance(t, Real) and t > 0
                 , "TONE_DETECT_TIMEOUT must be a positive number of milliseconds.")
    verify_field(cfg.MESSAGE_LOOP_SHORT_DELAY, lambda d: isinstance(d, Real) and d >= 0
                 , "MESSAGE_LOOP_SHORT_DELAY must be a non-negative number of seconds.")
    verify_field(cfg.NUM_SHORT_DELAYS, lambda d: isinstance(d, int) and d >= 0
                 , "NUM_SHORT_DELAYS must be a non-negative integer.")
    verify_field(cfg.MESSAGE_LOOP_LONG_DELAY, lambda d: isinstance(d, Real) and d >= 0
                 , "MESSAGE_LOOP_LONG_DELAY must be a non-negative number of seconds.")
    verify_field(cfg.CANCEL_HELP_TIMEOUT, lambda t: isinstance(t, Real) and t >= 0
                 , "CANCEL_HELP_TIMEOUT must be a non-negative number of seconds.")
    verify_field(cfg.TRANSMIT_DELAY, lambda d: isinstance(d, Real) and d >= 0
                 , "TRANSMIT_DELAY must be a non-negative number of seconds.")

    cfg.RIGCTLD_ADDRESS = cfg_dict.get('RIGCTLD_ADDRESS', '127.0.0.1')
    cfg.RIGCTLD_PORT = cfg_dict.get('RIGCTLD_PORT', 4532)
    cfg.SWITCH_TO_MEM_MODE = cfg_dict.get('SWITCH_TO_MEM_MODE', True)
    cfg.DISABLE_PTT = cfg_dict.get('DISABLE_PTT', False)
    cfg.RIGCTLD_OPERATION_TIMEOUT = cfg_dict.get('RIGCTLD_OPERATION_TIMEOUT', 7)  # seconds

    verify_field(cfg.RIGCTLD_ADDRESS, lambda addr: isinstance(addr, str)
                 , "RIGCTLD_ADDRESS must be a string specifying an IP address.")
    verify_field(cfg.RIGCTLD_PORT, lambda p: isinstance(p, int) and p >= 0 and p < 65536
                                                , "RIGCTLD_PORT must be a valid port number.")
    verify_field(cfg.SWITCH_TO_MEM_MODE, lambda b: isinstance(b, bool), 'SWITCH_TO_MEM_MODE must be "true" or "false"')
    verify_field(cfg.DISABLE_PTT, lambda b: isinstance(b, bool), 'DISABLE_PTT must be "true" or "false"')
    verify_field(cfg.RIGCTLD_OPERATION_TIMEOUT, lambda t: isinstance(t, Real) and t > 0
                 , "RIGCTLD_OPERATION_TIMEOUT must be a positive number of seconds.")

    cfg.LPZ_SAMPLING_PERIOD = cfg_dict.get('LPZ_SAMPLING_PERIOD')  # ms
    cfg.LPZ_TOTAL_SAMPLES = cfg_dict.get('LPZ_TOTAL_SAMPLES')  # number of samples
    cfg.LPZ_REQUIRED_ZERO_COUNT = cfg_dict.get('LPZ_REQUIRED_ZERO_COUNT')  # number of positive samples to conclude LPZ
    cfg.LPZ_MAX_ZERO_COUNT = cfg_dict.get(
        'LPZ_MAX_ZERO_COUNT')  # the number of samples that must be exceeded to conclude a false positive

    verify_field(cfg.LPZ_SAMPLING_PERIOD, lambda t: isinstance(t, Real) and t > 0
                 , "LPZ_SAMPLING_PERIOD must be a positive number of milliseconds.")
    verify_field(cfg.LPZ_TOTAL_SAMPLES, lambda n: isinstance(n, int) and n > 0
                 , "LPZ_TOTAL_SAMPLES must be a positive integer.")
    verify_field(cfg.LPZ_REQUIRED_ZERO_COUNT, lambda n: isinstance(n, int) and n > 0
                 , "LPZ_REQUIRED_ZERO_COUNT must be a positive integer.")
    verify_field(cfg.LPZ_MAX_ZERO_COUNT, lambda n: isinstance(n, int) and n > 0
                 , "LPZ_MAX_ZERO_COUNT must be a positive integer.")

    cfg.DCD_SAMPLING_PERIOD = cfg_dict.get('DCD_SAMPLING_PERIOD', 200)  # ms
    cfg.DCD_REQ_CONSEC_ZEROES = cfg_dict.get('DCD_REQ_CONSEC_ZEROES',
                                             6)  # number of consecutive zero samples to conclude silence

    verify_field(cfg.DCD_SAMPLING_PERIOD, lambda t: isinstance(t, Real) and t >= 0
                 , "DCD_SAMPLING_PERIOD must be a non-negative number of milliseconds.")
    verify_field(cfg.DCD_REQ_CONSEC_ZEROES, lambda n: isinstance(n, int) and n >= 0
                 , "DCD_REQ_CONSEC_ZEROES must be a non-negative integer.")

    cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING = cfg_dict.get('OUTPUT_AUDIO_DEVICE_SUBSTRING', None)
    verify_field(cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING, lambda s: s is None or isinstance(s, str)
                 , "OUTPUT_AUDIO_DEVICE_SUBSTRING must be a string or left unspecified.")

    cfg.DEBUG_MODE = cfg_dict.get('DEBUG_MODE', False)
    if cfg.DEBUG_MODE:
        cfg.DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING = cfg_dict.get('DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING', None)
        if cfg.DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING is not None:
            cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING = cfg.DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING
        cfg.USING_HAMLIB_DUMMY = cfg_dict.get('USING_HAMLIB_DUMMY', False)
        if cfg.USING_HAMLIB_DUMMY:
            cfg.SWITCH_TO_MEM_MODE = False
            cfg.DCD_REQ_CONSEC_ZEROES = 0
        else:
            cfg.DISABLE_PTT = True

    cfg.AUDIO_DIRECTORY = Path("audio/")
    cfg.SYS_CALLING_HELP_PATH = cfg.AUDIO_DIRECTORY / "system_is_calling_help.wav"
    cfg.LPZ_DETECTED_PATH = cfg.AUDIO_DIRECTORY / "lpz_detected.wav"
    cfg.ALERT_CANCELLED_PATH = cfg.AUDIO_DIRECTORY / "alert_cancelled.wav"
    cfg.BEEP_PATH = cfg.AUDIO_DIRECTORY / "beep.wav"
    cfg.ASCENDING_BEEP_PATH = cfg.AUDIO_DIRECTORY / "ascending_beep.wav"
    cfg.CALL_SIGN_PATH = cfg.AUDIO_DIRECTORY / "call_sign.wav"
    cfg.REPEATER_NAME_DIRECTORY = cfg.AUDIO_DIRECTORY / "repeater_name/"

    cfg.NOT_IN_ALERT_FLAG_PATH = Path("not_in_alert")

    return cfg


if __name__ == '__main__':
    Path("logs/").mkdir(exist_ok=True)
    logger.init("logs/log_file.log", level=logger.DEBUG)
    try:
        cfg = parse_cfg("arms_config.toml")
        arms = ARMS(cfg)
        arms.begin_operation()
    except TypeError:
        logger.exception("Error parsing configuration.")
