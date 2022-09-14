import re
import threading
from functools import reduce
from types import SimpleNamespace

import lovely_logger as logging
import time
import toml
from enum import Enum, auto
from itertools import cycle
from pathlib import Path
from time import sleep
from typing import Union, Dict
from numbers import Real

from audio_utils import Tone, wait_for_dtmf_tone, wait_for_dtmf_seq, wait_for_dtmf_seq_predicate, read_dtmf, load, init_io, play
from rig_controller import RigController, PTT


class ARMS:
    def __init__(self, cfg):
        self._cfg = cfg
        self._rigctlr = RigController(self._cfg.RIGCTLD_ADDRESS, self._cfg.RIGCTLD_PORT
                                      , self._cfg.RIGCTLD_OPERATION_TIMEOUT, disable_ptt=self._cfg.DISABLE_PTT
                                      , switch_to_mem_mode=self._cfg.SWITCH_TO_MEM_MODE)

    def begin_operation(self):
        self._rigctlr.set_ptt(PTT.RX)

        if self._cfg.INVALID_CONFIGURATION:
            self._broadcast_errors()
            return

        self._init_audio_io()
        self._load_audio_files()
        self._set_not_in_alert_flag(True)

        logging.info("ARMS is beginning operation.")
        while True:
            for ch in range(6, self._cfg.LAST_CHANNEL + 1):
                self._rigctlr.switch_channel(ch)
                tone = wait_for_dtmf_tone(self._cfg.TONE_DETECT_REC_LENGTH/1000, Tone.ZERO, Tone.HASH)
                if tone is not None:
                    self._set_not_in_alert_flag(False)
                    if self._detect_long_tone(tone):
                        if tone == Tone.ZERO:
                            self._alert_procedure(ch)
                        elif tone == Tone.HASH:
                            self._test_procedure(ch)
                        logging.info("Returning to normal (scanning) operation.")
                    self._set_not_in_alert_flag(True)

    def _set_not_in_alert_flag(self, not_in_alert: bool):
        try:
            if not_in_alert:
                self._cfg.NOT_IN_ALERT_FLAG_PATH.touch(exist_ok=True)
            else:
                self._cfg.NOT_IN_ALERT_FLAG_PATH.unlink()  # missing_ok argument requires python 3.8.
        except Exception:
            logging.exception("Error " + ("creating" if not_in_alert else "removing") + " not_in_alert flag file."
                                                                                       " Continuing operation.")

    def _broadcast_errors(self):
        logging.critical("ARMS has detected configuration errors. Broadcasting messages on alert channel.")
        self._init_audio_io(output_only=True)
        while True:
            self._transmit_files(self._cfg.ARMS_BOOT_ERROR_PATH)
            sleep(60)

    def _alert_procedure(self, ch: int):
        logging.info(f"Entering alert procedure; channel: {ch}.")
        self._transmit_files(*self._cfg.PARAGRAPHS.ADVISE_CALLER_HEARD)

        class LoopingBehavior(Enum):
            INITIAL_ALERT = auto()
            HANDLING_DELAY_SHORT = auto()
            HANDLING_DELAY_MODERATE = auto()
            HANDLING_DELAY_LONG = auto()
            IC_DEFINED = auto()

        def init_alert_transmit_procedure():
            logging.info("Playing initial information on alert channel.")
            self._transmit_files(*self._cfg.PARAGRAPHS.INITIAL_ALERT, self._repeater_name_path(ch))

        def handling_delay_base_transmit_procedure(looping_behavior: LoopingBehavior):
            if looping_behavior == LoopingBehavior.HANDLING_DELAY_SHORT:
                delay_length_str = "short"
                paragraph = self._cfg.PARAGRAPHS.SHORT_DELAY
            elif looping_behavior == looping_behavior.HANDLING_DELAY_MODERATE:
                delay_length_str = "moderate"
                paragraph = self._cfg.PARAGRAPHS.MODERATE_DELAY
            elif looping_behavior == looping_behavior.HANDLING_DELAY_LONG:
                delay_length_str = "long"
                paragraph = self._cfg.PARAGRAPHS.LONG_DELAY
            else:
                raise ValueError
            logging.info("Playing ARMS_GOING_TO_CALLING_CHANNEL on alert channel.")
            self._transmit_files(*self._cfg.PARAGRAPHS.ARMS_GOING_TO_CALLING_CHANNEL)
            logging.info(f"Announcing {delay_length_str} delay on calling channel.")
            self._rigctlr.switch_channel(ch)
            self._transmit_files(*paragraph)
            logging.info("Playing ARMS_IS_BACK_ON_ALERT_CHANNEL on alert channel.")
            self._rigctlr.switch_channel(1)
            self._transmit_files(*self._cfg.PARAGRAPHS.ARMS_IS_BACK_ON_ALERT_CHANNEL)
            logging.info(f"Announcing {delay_length_str} delay on alert channel.")
            self._transmit_files(*paragraph)

        def ic_defined_transmit_procedure(op_id: int):
            self._transmit_files(*self._cfg.PARAGRAPHS.IC_DEFINED, self._operator_name_path(op_id))

        delays_dict = {LoopingBehavior.INITIAL_ALERT: [self._cfg.INITIAL_ALERT_SHORT_DELAY_LENGTH] * self._cfg.INITIAL_ALERT_NUM_SHORT_DELAYS + [self._cfg.INITIAL_ALERT_LONG_DELAY_LENGTH]
            , LoopingBehavior.HANDLING_DELAY_SHORT: [self._cfg.SHORT_DELAY_MESSAGE_LOOP_LENGTH]
            , LoopingBehavior.HANDLING_DELAY_MODERATE: [self._cfg.MODERATE_DELAY_MESSAGE_LOOP_LENGTH]
            , LoopingBehavior.HANDLING_DELAY_LONG: [self._cfg.LONG_DELAY_MESSAGE_LOOP_LENGTH]
            , LoopingBehavior.IC_DEFINED: [self._cfg.IC_DEFINED_MESSAGE_LOOP_LENGTH]}

        info_transmit_procedure_dict = {LoopingBehavior.INITIAL_ALERT: init_alert_transmit_procedure
                                        , LoopingBehavior.IC_DEFINED: ic_defined_transmit_procedure}
        for b in {LoopingBehavior.HANDLING_DELAY_SHORT, LoopingBehavior.HANDLING_DELAY_MODERATE, LoopingBehavior.HANDLING_DELAY_LONG}:
            info_transmit_procedure_dict[b] = lambda local_b=b: handling_delay_base_transmit_procedure(local_b)

        class State(Enum):
            PLAYING_INFO = auto()
            WAITING = auto()
        states_iter = cycle(State)

        cur_looping_data = SimpleNamespace(delays=delays_dict[LoopingBehavior.INITIAL_ALERT], delay_index=0
                                           , info_transmit_procedure=init_alert_transmit_procedure
                                           , states_iter=cycle(State))

        def set_looping_data(looping_behavior: LoopingBehavior, reset_states_iter=True, *transmit_args):
            cur_looping_data.delays = delays_dict[looping_behavior]
            cur_looping_data.delay_index = 0
            cur_looping_data.info_transmit_procedure = lambda: info_transmit_procedure_dict[looping_behavior](*transmit_args)
            if reset_states_iter:
                cur_looping_data.states_iter = cycle(State)

        state = next(states_iter)
        remain_at_same_state = True
        self._rigctlr.switch_channel(1)
        while True:
            if remain_at_same_state:
                remain_at_same_state = False
            else:
                state = next(states_iter)
            if state == State.PLAYING_INFO:
                cur_looping_data.info_transmit_procedure()
            elif state == State.WAITING:
                logging.info("Awaiting command on channel 1.")
                seq = wait_for_dtmf_seq(cur_looping_data.delays[cur_looping_data.delay_index], False
                                         , "111", "222", "333", "444", "000", "*")
                if seq == "000":
                    logging.info("000 detected. Asking for confirmation before cancelling alert.")
                    self._transmit_files(*self._cfg.PARAGRAPHS.ALERT_CANCEL_CONFIRM)
                    if wait_for_dtmf_seq(self._cfg.CONFIRM_CANCEL_ALERT_TIMEOUT, False, "000") == "000":
                        logging.info("Confirmation via 000 detected. Cancelling alert procedure.")
                        logging.info("Acknowledging cancellation on alert channel.")
                        self._transmit_files(*self._cfg.PARAGRAPHS.ALERT_CANCELLED, self._repeater_name_path(ch), *self._cfg.PARAGRAPHS.ARMS_RETURNING_NORMAL_OP)
                        logging.info("Acknowledging cancellation in calling channel.")
                        self._rigctlr.switch_channel(ch)
                        self._transmit_files(*self._cfg.PARAGRAPHS.ALERT_CANCELLED, self._repeater_name_path(ch), *self._cfg.PARAGRAPHS.ARMS_RETURNING_NORMAL_OP)
                        return
                    else:
                        logging.info("Timeout reached while listening for confirmation to cancel alert. ARMS will"
                                     " remain in its current state.")
                        remain_at_same_state = True
                        continue
                elif seq == "111":
                    logging.info("111 detected. Switching to initial alert announcements on channel 1.")
                    set_looping_data(LoopingBehavior.INITIAL_ALERT)
                    continue
                elif seq == "222":
                    logging.info("222 detected. Initiating short delay announcements.")
                    set_looping_data(LoopingBehavior.HANDLING_DELAY_SHORT)
                    continue
                elif seq == "333":
                    logging.info("333 detected. Initiating moderate delay announcements.")
                    set_looping_data(LoopingBehavior.HANDLING_DELAY_MODERATE)
                    continue
                elif seq == "444":
                    logging.info("444 detected. Initiating long delay announcements.")
                    set_looping_data(LoopingBehavior.HANDLING_DELAY_LONG)
                    continue
                elif seq == "*":
                    logging.info("* detected. Initiating operator identification.")
                    op_id = self._detect_op_id()
                    if op_id is None:
                        logging.info("Timeout reached while listening for operator ID.")
                        self._transmit_files(*self._cfg.PARAGRAPHS.IC_CODE_TIMED_OUT)
                    elif op_id is False:
                        logging.info("Invalid operator ID detected.")
                        self._transmit_files(*self._cfg.PARAGRAPHS.IC_CODE_INVALID)
                    else:
                        logging.info("Detected ID: {:03d}.".format(op_id))
                        if self._cfg.OPERATORS[op_id]:
                            logging.info("The operator ID detected is active. Announcing this operator as in-command.")
                            set_looping_data(LoopingBehavior.IC_DEFINED, True, op_id)
                        else:
                            logging.info("The operator ID detected is NOT active.")
                            self._transmit_files(*self._cfg.PARAGRAPHS.IC_CODE_INVALID)
                        continue
                    logging.info("An operator was not successfully set as IC. ARMS will remain in its existing state.")
                    remain_at_same_state = True
                    continue
                cur_looping_data.delay_index = (cur_looping_data.delay_index + 1) % len(cur_looping_data.delays)

    def _test_procedure(self, ch):
        logging.info(f"Entering test procedure on channel {ch}.")
        self._transmit_files(*self._cfg.PARAGRAPHS.ENTER_OPERATOR_CODE)
        if wait_for_dtmf_tone(self._cfg.TESTING_STAR_DETECT_TIMEOUT, Tone.STAR) == Tone.STAR:
            op_id = self._detect_op_id()
            if op_id not in {None, False} and self._cfg.OPERATORS[op_id]:
                logging.info("Valid and active ID detected: {:03d}. Transmitting testing message on calling channel.".format(op_id))
                self._transmit_files(self._operator_name_path(op_id), *self._cfg.PARAGRAPHS.TESTING)
                sleep(2)
                logging.info("Transmitting testing message on alert channel.")
                self._rigctlr.switch_channel(1)
                self._transmit_files(self._operator_name_path(op_id), *self._cfg.PARAGRAPHS.TESTING)
            elif op_id is False:
                logging.info("Invalid or inactive ID detected. Transmitting message indicating this.")
                self._transmit_files(*self._cfg.PARAGRAPHS.TESTING_CODE_INVALID)
            elif op_id is None:
                logging.info("Timed out before detecting pound and operator code. Transmitting message indicating this.")
                self._transmit_files(*self._cfg.PARAGRAPHS.TESTING_CODE_TIMED_OUT)
        else:
            logging.info("Timed out before detecting '*'. Transmitting message indicating this.")
            self._transmit_files(*self._cfg.PARAGRAPHS.TESTING_CODE_TIMED_OUT)

    def _transmit_files(self, *filepaths):
        self._wait_for_silence()
        logging.info("Transmitting audio.")
        self._rigctlr.set_ptt(PTT.TX)
        sleep(self._cfg.TRANSMIT_DELAY)
        for path in filepaths:
            play(path)
        self._rigctlr.set_ptt(PTT.RX)

    def _wait_for_silence(self):
        logging.info(f"Waiting for silence. "
                     f"({self._cfg.DCD_REQ_CONSEC_ZEROES} consecutive zeroes,"
                     f" {self._cfg.DCD_SAMPLING_PERIOD} ms sampling period.)")
        consec_dcd_0_count = 0
        while True:
            last_sample_time = time.time()
            if self._rigctlr.get_dcd_is_open():
                consec_dcd_0_count = 0
            else:
                consec_dcd_0_count += 1
            if consec_dcd_0_count >= self._cfg.DCD_REQ_CONSEC_ZEROES:
                break
            sleep_time = last_sample_time + self._cfg.DCD_SAMPLING_PERIOD/1000 - time.time()
            if sleep_time > 0:
                sleep(sleep_time)

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
                tone = wait_for_dtmf_tone(timeout_seconds, *tones)
                if tone is not None:
                    with status.cond:
                        status.awaiting_silence = False
                        status.cond.notify()
                    logging.info("Tone detected before final timeout was started.")
                    silence_waiting_thread.join()
                    return tone
            else:
                logging.info("Silence criteria reached. Starting final timeout.")
                with status.cond:
                    timeout = status.deadline - time.time()
                silence_waiting_thread.join()
                return wait_for_dtmf_tone(max(timeout, 0), *tones)

    def _repeater_name_path(self, ch: int):
        return self._cfg.REPEATER_NAME_DIRECTORY / "{:02d}.wav".format(ch)

    def _operator_name_path(self, op_id: int):
        return self._cfg.OPERATOR_NAME_DIRECTORY / "{:03d}.wav".format(op_id)

    def _load_audio_files(self):
        paragraph_files = set()
        for par in self._cfg.REQUIRED_PARAGRAPHS:
            for file in self._cfg.PARAGRAPHS.__dict__[par]:
                paragraph_files.add(file)
        for file in paragraph_files:
            load(file)
        for ch in range(6, self._cfg.LAST_CHANNEL + 1):
            load(self._repeater_name_path(ch))
        for op_id, active in self._cfg.OPERATORS.items():
            if active:
                load(self._operator_name_path(op_id))

    def _init_audio_io(self, output_only=False):
        """
        Audio devices like to be unavailable through sounddevice the first time ARMS tries to use them after a reboot.
        Thus, we try to initialize audio a few times before declaring defeat.
        """
        for i in range(4):
            try:
                init_io(self._cfg.INPUT_AUDIO_DEVICE_SUBSTRING
                        , self._cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING, output_only)
            except Exception:
                if i < 3:
                    sleep(3)
                else:
                    raise

    def _sleep_millis(self, millis: float):
        sleep(millis / 1000)

    def _detect_long_tone(self, tone: Tone):
        pos_sample_count = 0
        start_time = time.time()
        for i in range(self._cfg.LONG_TONE_TOTAL_SAMPLES):
            sleep_ms = i * self._cfg.LONG_TONE_SAMPLING_PERIOD - 1000 * (time.time() - start_time)
            if sleep_ms > 0:
                self._sleep_millis(sleep_ms)
            if read_dtmf() == tone:
                pos_sample_count += 1
        return pos_sample_count >= self._cfg.LONG_TONE_REQUIRED_POSITIVE_SAMPLES and pos_sample_count <= self._cfg.LONG_TONE_MAX_POSITIVE_SAMPLES

    def _detect_op_id(self) -> Union[int, bool, None]:
        op_id_regex = re.compile(r"#\d{1,3}")

        def validity(s: str) -> Union[int, bool, None]:
            if op_id_regex.match(s) is None:
                return None
            substr = s[1:]
            if len(substr) == 1 and int(substr) % 2 == 1:
                return False
            elif len(substr) == 2 and int(substr[1]) % 2 == 0:
                return False
            elif len(substr) == 3:
                op_id = int(substr)
                return op_id if _valid_id(op_id) else False

        match = wait_for_dtmf_seq_predicate(max_rec_length=self._cfg.OPERATOR_ID_TIMEOUT, max_seq_length=4
                                           , ignore_repeat_tones=True
                                           , predicate=lambda s: validity(s) is not None)
        return validity(match) if match is not None else None


def _valid_id(id: int):
    if id < 16 or id > 894:
        return False
    hundreds_digit = (id // 100) % 10
    if hundreds_digit % 2 == 1:
        return False
    tens_digit = (id // 10) % 10
    if tens_digit % 2 == 0:
        return False
    units_digit = id % 10
    return units_digit == (tens_digit + 5) % 10


def parse_cfg(cfg_path):
    cfg_dict = toml.load(cfg_path)
    cfg = SimpleNamespace()

    cfg.DISABLE_ERROR_BROADCASTING = cfg_dict.get("DISABLE_ERROR_BROADCASTING", False)
    cfg.INVALID_CONFIGURATION = False

    def verify_field(value, predicate, exception_msg, raise_exception_on_invalid=False):
        if not predicate(value):
            if raise_exception_on_invalid or cfg.DISABLE_ERROR_BROADCASTING:
                raise TypeError(exception_msg)
            else:
                cfg.INVALID_CONFIGURATION = True
                logging.critical(exception_msg)
                return False
        return True

    verify_field(cfg.DISABLE_ERROR_BROADCASTING, lambda b: isinstance(b, bool), "DISABLE_ERROR_BROADCASTING"
                                                                                " must be 'true' or 'false'.", True)

    cfg.LAST_CHANNEL = cfg_dict['LAST_CHANNEL']
    cfg.TONE_DETECT_REC_LENGTH = cfg_dict.get('TONE_DETECT_REC_LENGTH')  # ms, length of recording while scanning.
    #cfg.CANCEL_HELP_TIMEOUT = cfg_dict.get('CANCEL_HELP_TIMEOUT')
    cfg.CONFIRM_CANCEL_ALERT_TIMEOUT = cfg_dict.get('CONFIRM_CANCEL_ALERT_TIMEOUT', 8)
    cfg.TESTING_STAR_DETECT_TIMEOUT = cfg_dict.get('TESTING_STAR_DETECT_TIMEOUT', 10)
    cfg.OPERATOR_ID_TIMEOUT = cfg_dict.get('OPERATOR_ID_TIMEOUT', 7)
    cfg.TRANSMIT_DELAY = cfg_dict.get('TRANSMIT_DELAY',
                                        1.5)  # seconds. Delay after activating PTT and before playing files.
    last_channel_valid = verify_field(cfg.LAST_CHANNEL, lambda ch: isinstance(ch, int) and ch >= 6
                 , "LAST_CHANNEL must be an integer greater than or equal to 6.")
    verify_field(cfg.TONE_DETECT_REC_LENGTH, lambda t: isinstance(t, Real) and t >= 50
                 , "TONE_DETECT_REC_LENGTH must be a number of milliseconds greater than or equal to 50.")
    #verify_field(cfg.CANCEL_HELP_TIMEOUT, lambda t: isinstance(t, Real) and t >= 0
                 #, "CANCEL_HELP_TIMEOUT must be a non-negative number of seconds.")
    verify_field(cfg.CONFIRM_CANCEL_ALERT_TIMEOUT, lambda t: isinstance(t, Real) and t > 0
                 , "CONFIRM_CANCEL_ALERT_TIMEOUT must be a positive number of seconds.")
    verify_field(cfg.TESTING_STAR_DETECT_TIMEOUT, lambda t: isinstance(t, Real) and t >= 0
                 , "TESTING_STAR_DETECT_TIMEOUT must be a non-negative number of seconds.")
    verify_field(cfg.OPERATOR_ID_TIMEOUT, lambda t: isinstance(t, Real) and t >= 0
                 , "OPERATOR_ID_TIMEOUT must be a non-negative number of seconds.")
    if not verify_field(cfg.TRANSMIT_DELAY, lambda d: isinstance(d, Real) and d >= 0
                 , "TRANSMIT_DELAY must be a non-negative number of seconds."):
        cfg.TRANSMIT_DELAY = 1

    cfg.INITIAL_ALERT_SHORT_DELAY_LENGTH = cfg_dict.get('INITIAL_ALERT_SHORT_DELAY_LENGTH', 5)  # seconds
    cfg.INITIAL_ALERT_NUM_SHORT_DELAYS = cfg_dict.get('INITIAL_ALERT_NUM_SHORT_DELAYS', 2)
    cfg.INITIAL_ALERT_LONG_DELAY_LENGTH = cfg_dict.get('INITIAL_ALERT_LONG_DELAY_LENGTH', 300)  # seconds
    cfg.SHORT_DELAY_MESSAGE_LOOP_LENGTH = cfg_dict.get('SHORT_DELAY_MESSAGE_LOOP_LENGTH', 60)
    cfg.MODERATE_DELAY_MESSAGE_LOOP_LENGTH = cfg_dict.get('MODERATE_DELAY_MESSAGE_LOOP_LENGTH', 120)
    cfg.LONG_DELAY_MESSAGE_LOOP_LENGTH = cfg_dict.get('LONG_DELAY_MESSAGE_LOOP_LENGTH', 120)
    cfg.IC_DEFINED_MESSAGE_LOOP_LENGTH = cfg_dict.get('IC_DEFINED_MESSAGE_LOOP_LENGTH', 120)

    verify_field(cfg.INITIAL_ALERT_SHORT_DELAY_LENGTH, lambda d: isinstance(d, Real) and d >= 0
                 , "INITIAL_ALERT_SHORT_DELAY_LENGTH must be a non-negative number of seconds.")
    verify_field(cfg.INITIAL_ALERT_NUM_SHORT_DELAYS, lambda d: isinstance(d, int) and d >= 0
                 , "INITIAL_ALERT_NUM_SHORT_DELAYS must be a non-negative integer.")
    verify_field(cfg.INITIAL_ALERT_LONG_DELAY_LENGTH, lambda d: isinstance(d, Real) and d >= 0
                 , "INITIAL_ALERT_LONG_DELAY_LENGTH must be a non-negative number of seconds.")
    verify_field(cfg.SHORT_DELAY_MESSAGE_LOOP_LENGTH, lambda d: isinstance(d, Real) and d >= 0
                 , "SHORT_DELAY_MESSAGE_LOOP_LENGTH must be a non-negative number of seconds.")
    verify_field(cfg.MODERATE_DELAY_MESSAGE_LOOP_LENGTH, lambda d: isinstance(d, Real) and d >= 0
                 , "MODERATE_DELAY_MESSAGE_LOOP_LENGTH must be a non-negative number of seconds.")
    verify_field(cfg.LONG_DELAY_MESSAGE_LOOP_LENGTH, lambda d: isinstance(d, Real) and d >= 0
                 , "LONG_DELAY_MESSAGE_LOOP_LENGTH must be a non-negative number of seconds.")
    verify_field(cfg.IC_DEFINED_MESSAGE_LOOP_LENGTH, lambda d: isinstance(d, Real) and d >= 0
                 , "IC_DEFINED_MESSAGE_LOOP_LENGTH must be a non-negative number of seconds.")

    cfg.RIGCTLD_ADDRESS = cfg_dict.get('RIGCTLD_ADDRESS', '127.0.0.1')
    cfg.RIGCTLD_PORT = cfg_dict.get('RIGCTLD_PORT', 4532)
    cfg.SWITCH_TO_MEM_MODE = cfg_dict.get('SWITCH_TO_MEM_MODE', True)
    cfg.DISABLE_PTT = cfg_dict.get('DISABLE_PTT', False)
    cfg.RIGCTLD_OPERATION_TIMEOUT = cfg_dict.get('RIGCTLD_OPERATION_TIMEOUT', 7)  # seconds

    verify_field(cfg.RIGCTLD_ADDRESS, lambda addr: isinstance(addr, str)
                 , "RIGCTLD_ADDRESS must be a string specifying an IP address.", True)
    verify_field(cfg.RIGCTLD_PORT, lambda p: isinstance(p, int) and p >= 0 and p < 65536
                                                , "RIGCTLD_PORT must be a valid port number.", True)
    verify_field(cfg.SWITCH_TO_MEM_MODE, lambda b: isinstance(b, bool), 'SWITCH_TO_MEM_MODE must be "true" or "false"', True)
    verify_field(cfg.DISABLE_PTT, lambda b: isinstance(b, bool), 'DISABLE_PTT must be "true" or "false"')
    verify_field(cfg.RIGCTLD_OPERATION_TIMEOUT, lambda t: isinstance(t, Real) and t > 0
                 , "RIGCTLD_OPERATION_TIMEOUT must be a positive number of seconds.")

    cfg.LONG_TONE_SAMPLING_PERIOD = cfg_dict.get('LONG_TONE_SAMPLING_PERIOD')  # ms
    cfg.LONG_TONE_TOTAL_SAMPLES = cfg_dict.get('LONG_TONE_TOTAL_SAMPLES')  # number of samples
    cfg.LONG_TONE_REQUIRED_POSITIVE_SAMPLES = cfg_dict.get('LONG_TONE_REQUIRED_POSITIVE_SAMPLES')  # number of positive samples to conclude LPZ
    cfg.LONG_TONE_MAX_POSITIVE_SAMPLES = cfg_dict.get(
        'LONG_TONE_MAX_POSITIVE_SAMPLES')  # the number of samples that must be exceeded to conclude a false positive

    verify_field(cfg.LONG_TONE_SAMPLING_PERIOD, lambda t: isinstance(t, Real) and t >= 100
                 , "LONG_TONE_SAMPLING_PERIOD must be a number of milliseconds greater than or equal to 100.")
    verify_field(cfg.LONG_TONE_TOTAL_SAMPLES, lambda n: isinstance(n, int) and n > 0
                 , "LONG_TONE_TOTAL_SAMPLES must be a positive integer.")
    verify_field(cfg.LONG_TONE_REQUIRED_POSITIVE_SAMPLES, lambda n: isinstance(n, int) and n > 0
                 , "LONG_TONE_REQUIRED_POSITIVE_SAMPLES must be a positive integer.")
    verify_field(cfg.LONG_TONE_MAX_POSITIVE_SAMPLES, lambda n: isinstance(n, int) and n > 0
                 , "LONG_TONE_MAX_POSITIVE_SAMPLES must be a positive integer.")

    cfg.DCD_SAMPLING_PERIOD = cfg_dict.get('DCD_SAMPLING_PERIOD', 200)  # ms
    cfg.DCD_REQ_CONSEC_ZEROES = cfg_dict.get('DCD_REQ_CONSEC_ZEROES',
                                             6)  # number of consecutive zero samples to conclude silence

    if not verify_field(cfg.DCD_SAMPLING_PERIOD, lambda t: isinstance(t, Real) and t >= 0
                 , "DCD_SAMPLING_PERIOD must be a non-negative number of milliseconds."):
        cfg.DCD_SAMPLING_PERIOD = 200
    if not verify_field(cfg.DCD_REQ_CONSEC_ZEROES, lambda n: isinstance(n, int) and n >= 0
                 , "DCD_REQ_CONSEC_ZEROES must be a non-negative integer."):
        cfg.DCD_REQ_CONSEC_ZEROES = 5

    cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING = cfg_dict.get('OUTPUT_AUDIO_DEVICE_SUBSTRING', None)
    verify_field(cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING, lambda s: s is None or isinstance(s, str)
                 , "OUTPUT_AUDIO_DEVICE_SUBSTRING must be a string or left unspecified.", True)
    cfg.INPUT_AUDIO_DEVICE_SUBSTRING = cfg_dict.get('INPUT_AUDIO_DEVICE_SUBSTRING', None)
    verify_field(cfg.INPUT_AUDIO_DEVICE_SUBSTRING, lambda s: s is None or isinstance(s, str)
                 , "INPUT_AUDIO_DEVICE_SUBSTRING must be a string or left unspecified.")

    cfg.DEBUG_MODE = cfg_dict.get('DEBUG_MODE', False)
    if cfg.DEBUG_MODE:
        cfg.DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING = cfg_dict.get('DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING', None)
        if cfg.DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING is not None:
            cfg.OUTPUT_AUDIO_DEVICE_SUBSTRING = cfg.DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING
        cfg.DEBUG_INPUT_AUDIO_DEVICE_SUBSTRING = cfg_dict.get('DEBUG_INPUT_AUDIO_DEVICE_SUBSTRING', None)
        if cfg.DEBUG_OUTPUT_AUDIO_DEVICE_SUBSTRING is not None:
            cfg.INPUT_AUDIO_DEVICE_SUBSTRING = cfg.DEBUG_INPUT_AUDIO_DEVICE_SUBSTRING
        cfg.USING_HAMLIB_DUMMY = cfg_dict.get('USING_HAMLIB_DUMMY', False)
        if cfg.USING_HAMLIB_DUMMY:
            cfg.SWITCH_TO_MEM_MODE = False
            cfg.DCD_REQ_CONSEC_ZEROES = 0
        else:
            cfg.DISABLE_PTT = True

    cfg.AUDIO_DIRECTORY = Path("audio/")
    cfg.REPEATER_NAME_DIRECTORY = cfg.AUDIO_DIRECTORY / "repeater_name/"
    cfg.OPERATOR_NAME_DIRECTORY = cfg.AUDIO_DIRECTORY / "operator_name/"
    cfg.ARMS_BOOT_ERROR_PATH = cfg.AUDIO_DIRECTORY / "ARMS_boot_error.wav"

    def verify_path_readable(path: Union[Path, str], directory:Union[Path, None]=None):
        if not isinstance(path, Path) and not isinstance(path, str):
            return False
        if directory is not None:
            path = directory / path
        elif not isinstance(path, Path):
            path = Path(path)
        try:
            with path.open("r") as file:
                if not file.readable():
                    return False
        except IOError:
            return False
        return True

    verify_field(cfg.ARMS_BOOT_ERROR_PATH, verify_path_readable
                 , f"The necessary file '{cfg.ARMS_BOOT_ERROR_PATH}' is either missing or could not be read.", True)

    def verify_repeater_names():
        for ch in range(6, cfg.LAST_CHANNEL):
            if not verify_path_readable(cfg.REPEATER_NAME_DIRECTORY / "{:02d}.wav".format(ch)):
                return False
        return True

    if last_channel_valid:
        verify_field(None, lambda *args, **kwargs: verify_repeater_names(), f"One or more repeater name audio files either was not found in"
                                                    f" the directory {cfg.REPEATER_NAME_DIRECTORY}"
                                                    f" or could not be read.")

    cfg.OPERATORS = cfg_dict.get("OPERATORS", {})

    def operators_predicate(operators_dict: Dict):
        for id_str, active in operators_dict.items():
            if not id_str.isdigit() or len(id_str) != 3 or not _valid_id(int(id_str)) or not isinstance(active, bool):
                return False
        return True

    valid_operators = verify_field(cfg.OPERATORS, operators_predicate
                 , """
Active operator IDs should be specified as follows:
                 
[OPERATORS]
016 = true  # Your optional comment here for some active operator.
038 = false # Inactive operator.
...
 
The only valid operator numbers are:
016, 038, 050, 072, 094,
216, 238, 250, 272, 294,
416, 438, 450, 472, 494,
616, 638, 650, 672, 694,
816, 838, 850, 872, 894.

Operator ID mnl is valid if and only if m is even, n is odd, and l = (n + 5) mod 10.

                 """)
    cfg.OPERATORS = {int(id_str): active for id_str, active in cfg.OPERATORS.items()}

    def verify_operator_audio_files():
        for op_id, active in cfg.OPERATORS.items():
            if active and not verify_path_readable(cfg.OPERATOR_NAME_DIRECTORY / "{:03d}.wav".format(op_id)):
                return False
        return True

    if valid_operators:
        verify_field(None, lambda *args, **kwargs: verify_operator_audio_files(), f"At least one of the operator name audio files"
                                                        f" either could not be found in the directory"
                                                        f" {cfg.OPERATOR_NAME_DIRECTORY} or could not be read.")

    cfg.PARAGRAPHS = cfg_dict.get("PARAGRAPHS", {})
    cfg.REQUIRED_PARAGRAPHS = {"ADVISE_CALLER_HEARD", "INITIAL_ALERT", "IC_DEFINED", "SHORT_DELAY", "MODERATE_DELAY"
                               , "LONG_DELAY", "ALERT_CANCELLED", "ARMS_RETURNING_NORMAL_OP", "IC_CODE_TIMED_OUT"
                               , "IC_CODE_INVALID", "TESTING", "ENTER_OPERATOR_CODE", "TESTING_CODE_INVALID"
                               , "TESTING_CODE_TIMED_OUT", "ARMS_GOING_TO_CALLING_CHANNEL"
                               , "ARMS_IS_BACK_ON_ALERT_CHANNEL", "ALERT_CANCEL_CONFIRM"}
    common_paragraphs_error_str = """
An issue was detected with the paragraphs in the configuration. Please check for a specific message in a separate
logging entry earlier than this one.

Paragraphs should be specified as non-empty lists of strings representing the sequence of audio
files to be played as part of that paragraph. Each string should be a file path relative to the
audio directory. For example, "call_sign.wav" corresponds to "audio/call_sign.wav".
All such files must exist and be readable by ARMS. For example:

INITIAL_ALERT = ["ascending_beep.wav", "call_sign.wav", "lpz_detected.wav"]
IC_DEFINED = ["all_stations_standby.wav"]
...

The following paragraphs, and only the following paragraphs, must be defined:

""" + reduce(lambda s1, s2: s1 + s2 + "\n", cfg.REQUIRED_PARAGRAPHS, "")

    def paragraphs_predicate(paragraphs: Dict):
        if paragraphs.keys() != cfg.REQUIRED_PARAGRAPHS:
            logging.critical("There is a mismatch between paragraph names which were required and which were provided.")
            return False
        for path_seq in paragraphs.values():
            if len(path_seq) == 0:
                logging.critical("At least one of the paragraphs is empty.")
                return False
            for path in path_seq:
                if not verify_path_readable(path, cfg.AUDIO_DIRECTORY):
                    logging.critical(f"The path '{path}' is either invalid or corresponds to a file which cannot be read.")
                    return False
        return True
    verify_field(cfg.PARAGRAPHS, paragraphs_predicate
                 , common_paragraphs_error_str)
    cfg.PARAGRAPHS = SimpleNamespace(**{par_name: [cfg.AUDIO_DIRECTORY/path for path in paths] for par_name, paths in cfg.PARAGRAPHS.items()})

    cfg.NOT_IN_ALERT_FLAG_PATH = Path("not_in_alert")

    return cfg


if __name__ == '__main__':
    Path("logs/").mkdir(exist_ok=True)
    logging.init("logs/log_file.log", level=logging.INFO)
    try:
        cfg = parse_cfg("arms_config.toml")
        if cfg.DEBUG_MODE:
            logging.logger.setLevel(logging.DEBUG)
        arms = ARMS(cfg)
        arms.begin_operation()
    except TypeError:
        logging.exception("Error parsing configuration.")
        raise
