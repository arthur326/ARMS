from types import SimpleNamespace
import numpy
import sounddevice
import soundfile

_loaded_files = {}
_single_stream_data = SimpleNamespace(cur_stream=None, data_index=None)


def set_default_io(input_device=None, output_device=None):
    sounddevice.default.device = input_device, output_device
    if input_device is not None:
        sounddevice.check_input_settings(device=input_device)
    if output_device is not None:
        sounddevice.check_output_settings(device=output_device)


def load(filepath):
    data, samplerate = soundfile.read(filepath, dtype="float32")
    _loaded_files[filepath] = (data, samplerate)


def unload(filepath):
    if filepath in _loaded_files.keys():
        del _loaded_files[filepath]


def play(filepath, blocking=True):
    data, samplerate = _get_audio_data(filepath)
    sounddevice.play(data, samplerate, blocking=blocking)


def get_duration(filepath):
    if filepath in _loaded_files:
        data, samplerate = _loaded_files[filepath]
        return len(data)/samplerate
    with soundfile.SoundFile(filepath) as file:
        return file.frames/file.samplerate


def _get_audio_data(filepath):
    """
    Returns (data, samplerate). Uses saved result in _loaded_files if present; otherwise, reads from disc and does
    not save result in loaded_files.
    """
    return _loaded_files[filepath] if filepath in _loaded_files.keys() \
        else soundfile.read(filepath, dtype='float32')


def play_single_stream(filepath, finished_callback=None, device=None):
    """
    Uses at most one simultaneous stream to play the given audio file. Creates a new stream on every call.
    Executes the optional finished_callback function provided upon reaching the end of the file. Does not block.
    finished_callback must have the signature () -> None.
    """
    data, samplerate = _get_audio_data(filepath)
    _single_stream_data.data_index = 0

    def callback(outdata: numpy.ndarray, frames: int, time, status: sounddevice.CallbackFlags):
        i = _single_stream_data.data_index
        _single_stream_data.data_index += frames
        remaining_frames = len(data) - i
        outdata[:min(remaining_frames, frames)] = data[i:i+min(frames, remaining_frames)]
        if remaining_frames <= frames:
            outdata[remaining_frames:] = 0
            raise sounddevice.CallbackStop

    if _single_stream_data.cur_stream is not None:
        _single_stream_data.cur_stream.close()
    _single_stream_data.cur_stream = sounddevice\
        .OutputStream(samplerate=samplerate, callback=callback, device=device
                      , finished_callback=finished_callback, channels=2, dtype="float32")
    _single_stream_data.cur_stream.start()


def abort_single_stream_playback():
    if _single_stream_data.cur_stream is not None:
        _single_stream_data.cur_stream.abort()


def wait():
    sounddevice.wait()
