import RPi.GPIO as GPIO
from enum import Enum
from typing import Union


GPIO.setmode(GPIO.BCM)

input_pin_nums = [17, 27, 22, 23, 24]  # [Q1, Q2, Q3, Q4, StQ]
bin_digit_values = [1, 2, 4, 8]  # to have precomputed
for i in input_pin_nums:
    GPIO.setup(i, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)


# DTMF tones according to binary output of MT8770 chip.
class Tone(Enum):
    ONE = 1
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6
    SEVEN = 7
    EIGHT = 8
    NINE = 9
    ZERO = 10  # Interesting chip output
    STAR = 11
    POUND = 12
    HASH = 12
    A = 13
    B = 14
    C = 15
    D = 0


def read_dtmf():
    """
    Returns the DTMF tone immediately present, or None if one isn't present (per the StQ output).
    """
    tone_present = GPIO.input(input_pin_nums[4])
    if not tone_present:
        return None
    return read_last_dtmf()


def read_last_dtmf():
    """
    Returns the current or last DTMF tone, per the output of Q1--Q4. (Thus, some default value will
    be returned even if no tone has yet been heard by the chip.)
    """
    return Tone(sum([GPIO.input(input_pin_nums[i])*bin_digit_values[i] for i in range(4)]))


def get_next_dtmf(timeout_ms=None) -> Union[Tone, None]:
    wait_for_edge_args = [input_pin_nums[4], GPIO.RISING]
    kwargs = {}
    if timeout_ms is not None:
        if timeout_ms == 0:  # wait_for_edge doesn't like a timeout of 0, which can occur upon truncation.
            return None
        kwargs["timeout"] = timeout_ms
    # The default timeout is -1 in wait_for_edge, but as this is not stated in the documentation, we do not assume it...
    result = GPIO.wait_for_edge(input_pin_nums[4], GPIO.RISING, **kwargs)
    return read_last_dtmf() if result is not None else None
