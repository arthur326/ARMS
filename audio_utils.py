import atexit
import re
from enum import Enum, auto
from io import TextIOWrapper
from math import ceil
from threading import RLock, Condition
from types import SimpleNamespace
from typing import Union
import numpy
import sounddevice
import soundfile
import samplerate as sr
from subprocess import Popen, PIPE, STDOUT, DEVNULL
from time import time, sleep
import lovely_logger as logging
logger = logging.logger

_loaded_files = {}
_out_stream_data = SimpleNamespace(stream=None, cond=Condition(), playing_data=False, data=None, data_index=None)

_subprocess_copy = Popen(["multimon-ng", "-a", "DTMF", "-"], stdout=DEVNULL, stdin=PIPE, stderr=DEVNULL)
atexit.register(_subprocess_copy.kill)
_in_stream_data = SimpleNamespace(stream=None, lock=RLock(), piping_data=False, remaining_frames=0, proc=None)
_DETECTED_DTMF_PATTERN = re.compile(r"DTMF\s*:\s*(?P<value>[0-9A-D#*])\s*")
_SAFETY_WAIT_BUFFER = 0.005


def init_io(input_device=None, output_device=None):
    """
    Sets the input and output audio devices; intended to be called once and before other operations. Default devices
    will be used if unspecified.
    """
    sounddevice.default.device = input_device, output_device
    sounddevice.check_input_settings(device=input_device)
    sounddevice.check_output_settings(device=output_device)
    if _out_stream_data.stream is not None:
        _out_stream_data.stream.close()
    _out_stream_data.stream = sounddevice.OutputStream(device=output_device, channels=2, callback=_out_stream_callback
                                                       , finished_callback=_out_stream_finished_callback)
    _out_stream_data.stream.start()
    if _in_stream_data.stream is not None:
        _in_stream_data.stream.close()
    # multimon-ng native format is s16le, 22050 Hz, mono.
    _in_stream_data.stream = sounddevice.InputStream(device=input_device, dtype="<i2", samplerate=22050, channels=1,
                                                     callback=_in_stream_callback)
    _in_stream_data.stream.start()


def load(filepath):
    """
    Loads audio data into memory. Data in memory will be used instead of reading from disk unless unload is called.
    """
    data, samplerate = _read_audio_data(filepath)
    _loaded_files[filepath] = (data, samplerate)


def _read_audio_data(filepath, converter_type='sinc_medium'):
    """
    Reads the given file from disk and returns data, samplerate. If the source samplerate differs
    from the samplerate of the OutputStream, we resample the data.
    The converter_type to pass to libsamplerate, in case resampling is required, can be specified.
    """
    data, samplerate = soundfile.read(filepath, dtype='float32')
    if samplerate != _out_stream_data.stream.samplerate:
        data = sr.resample(data, _out_stream_data.stream.samplerate/samplerate, converter_type=converter_type)
        samplerate = _out_stream_data.stream.samplerate
        logger.info(f"{filepath} resampled at {samplerate} Hz. Converter type: {converter_type}.")
    return data, samplerate


def unload(filepath):
    if filepath in _loaded_files.keys():
        del _loaded_files[filepath]


def play(filepath, blocking=True):
    """
    Plays given file using the OutputStream created in init_io. Stops playback of anything else being played through
    this OutputStream. Blocks until playback is finished or interrupted if blocking is True.
    """
    data, samplerate = _get_audio_data(filepath)
    _out_stream_data.playing_data = False
    with _out_stream_data.cond:
        _out_stream_data.cond.notify_all()
        _out_stream_data.data = data
        _out_stream_data.data_index = 0
        _out_stream_data.playing_data = True
        if blocking:
            _out_stream_data.cond.wait()


def _get_audio_data(filepath):
    """
    Returns data, samplerate corresponding to audio file. Uses saved result in _loaded_files if present with correct
    samplerate; otherwise, reads from disc and does not save result in loaded_files.
    """
    if filepath in _loaded_files.keys():
        data, samplerate = _loaded_files[filepath]
        if samplerate == _out_stream_data.stream.samplerate:
            return data, samplerate
    return _read_audio_data(filepath)


def abort_playback():
    # Starvation could occur if lock is not FIFO here though would still be unlikely.
    with _out_stream_data.cond:
        _out_stream_data.playing_data = False
        _out_stream_data.cond.notify_all()


class Tone(Enum):
    """
    DTMF tones with values according to multimon-ng naming.
    """
    ZERO = '0'
    ONE = '1'
    TWO = '2'
    THREE = '3'
    FOUR = '4'
    FIVE = '5'
    SIX = '6'
    SEVEN = '7'
    EIGHT = '8'
    NINE = '9'
    STAR = '*'
    HASH = '#'
    POUND = HASH
    A = 'A'
    B = 'B'
    C = 'C'
    D = 'D'


def _out_stream_callback(outdata: numpy.ndarray, frames: int,
         time, status) -> None:
    if not _out_stream_data.playing_data:
        outdata[:] = 0
        return
    with _out_stream_data.cond:
        if not _out_stream_data.playing_data:
            outdata[:] = 0
            return
        i = _out_stream_data.data_index
        data = _out_stream_data.data
        frames_available = len(data) - i
        # Need to perform assignment with transposes in case mono data needs to be broadcasted to two channels.
        outdata[:min(frames, frames_available)].transpose()[:] = data[i:(i+min(frames, frames_available))].transpose()
        if frames >= frames_available:
            outdata[frames_available:] = 0
            _out_stream_data.playing_data = False
            _out_stream_data.cond.notify_all()
        else:
            _out_stream_data.data_index += frames


def _out_stream_finished_callback() -> None:
    """
    As far as the OutputStream we use is concerned, it is always active and should never be aborted. If it somehow is
    aborted, however, we notify any threads waiting for playback to finish. This will probably lead to an exception upon
    the next attempt to use the stream.
    """
    with _out_stream_data.cond:
        _out_stream_data.playing_data = False
        _out_stream_data.cond.notify_all()


def _in_stream_callback(indata: numpy.ndarray, frames: int,
                        time, status) -> None:
    if not _in_stream_data.piping_data:
        return
    with _in_stream_data.lock:
        if not _in_stream_data.piping_data:
            return
        if _in_stream_data.remaining_frames is not None:
            if frames >= _in_stream_data.remaining_frames:
                _in_stream_data.piping_data = False
                frames = _in_stream_data.remaining_frames
            _in_stream_data.remaining_frames -= frames
        _in_stream_data.proc.stdin.write(indata[:frames].tobytes(order='C'))
        if not _in_stream_data.piping_data:
            _in_stream_data.proc.stdin.close()


def wait_for_dtmf_seq_predicate(max_rec_length=None, predicate=lambda s: True, max_seq_length=5, ignore_repeat_tones=False) -> Union[str, None]:
    """
    Await a sequence of DTMF tones satisfying the provided predicate. If multiple matched become immediately available,
    the longest will be returned. An E will be present in the string before the first tone received if fewer than
    max_seq_length have been received.
    :param int max_seq_length: the number of most recent tones retained and examined for sequences matching the predicate.
    Matching sequences may be any non-empty substring of the retained sequence.
    :param Real max_rec_length: maximum amount of audio data to be analyzed, in seconds. None specifies unlimited.
    :param bool ignore_repeat_tones: if true, detected tones that are the same as the one most recently received will
    not be appended to the retained sequence of tones being analyzed for matches.
    """
    input_latency = _in_stream_data.stream.latency
    earliest_start = time() + input_latency + _SAFETY_WAIT_BUFFER
    if _in_stream_data.proc is not None:
        _in_stream_data.proc.kill()
    _in_stream_data.proc = Popen(["multimon-ng", "-a", "DTMF", "-"], stdout=PIPE, stdin=PIPE, stderr=STDOUT)
    wait_time = earliest_start - time()
    if wait_time > 0:
        sleep(wait_time)
    _in_stream_data.remaining_frames = None if max_rec_length is None\
        else ceil(max_rec_length * _in_stream_data.stream.samplerate)
    _in_stream_data.piping_data = True
    stdout = TextIOWrapper(_in_stream_data.proc.stdout, encoding="utf-8")
    current_seq = "E" * max_seq_length
    while True:
        line = stdout.readline()
        if line == "":
            return None
        match = _DETECTED_DTMF_PATTERN.match(line)
        if match is not None:
            tone_char = match.group("value")
            if ignore_repeat_tones and tone_char == current_seq[len(current_seq) - 1]:
                continue
            current_seq = current_seq[1:] + tone_char
            matching_seq = None
            for i in range(max_seq_length):
                candidate_seq = current_seq[i:]
                if predicate(candidate_seq):
                    matching_seq = candidate_seq
                    break
            if matching_seq is None:
                continue
            _in_stream_data.remaining_frames = 0  # Tell stream callback to stop in case we get starved acquiring lock.
            kill_process = False
            with _in_stream_data.lock:
                if _in_stream_data.piping_data:
                    kill_process = True
                    _in_stream_data.piping_data = False
            if kill_process:
                _in_stream_data.proc.kill()
                _in_stream_data.proc = None
            return matching_seq


def wait_for_dtmf_seq(max_rec_length=None, ignore_repeat_tones=False, *seqs) -> Union[str, None]:
    return wait_for_dtmf_seq_predicate(max_rec_length=max_rec_length, predicate=lambda s: s in seqs
                                       , max_seq_length=max(map(lambda s: len(s), seqs))
                                       , ignore_repeat_tones=ignore_repeat_tones)


def wait_for_dtmf_tone(max_rec_length=None, *tones) -> Union[Tone, None]:
    result = wait_for_dtmf_seq_predicate(max_rec_length, lambda s: s in map(lambda tone: tone.value, tones)
                                                         if len(tones) > 0 else lambda s: True
                                         , max_seq_length=1)
    return None if result is None else Tone(result)


def read_dtmf():
    return wait_for_dtmf_tone(0.040)
