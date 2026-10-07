from array import array
from types import SimpleNamespace
import struct
import unittest

from anima.adapters.audio.mixer import DiscordAudioMixer, MixingAudioSource, PCM_FRAME_BYTES
from anima.core.audio_ports import AudioInput, AudioLane


def pcm(value, samples=PCM_FRAME_BYTES // 2):
    return array("h", [value] * samples).tobytes()


class FakePCM:
    def __init__(self, frames, error=None):
        self.frames = list(frames)
        self.error = error
        self.cleaned = False

    def read(self):
        if self.error:
            raise self.error
        return self.frames.pop(0) if self.frames else b""

    def cleanup(self):
        self.cleaned = True


class Output:
    def __init__(self, playing=False):
        self.playing = playing
        self.sources = []

    def is_playing(self):
        return self.playing

    def play(self, source):
        self.sources.append(source)
        self.playing = True


class MixingAudioSourceTests(unittest.TestCase):
    def test_speech_ducks_music_and_recovers(self):
        mixer = MixingAudioSource(music_volume=0.5, ducking_volume=0.2)
        mixer.play_input(AudioInput(AudioLane.MUSIC, FakePCM([pcm(10_000)] * 2), lambda _: None))
        mixer.play_input(AudioInput(AudioLane.SPEECH, FakePCM([pcm(1_000)]), lambda _: None))
        self.assertEqual(struct.unpack_from("<h", mixer.read())[0], 3_000)
        self.assertEqual(struct.unpack_from("<h", mixer.read())[0], 5_000)

    def test_effect_overlays_without_ducking_and_all_lanes_mix(self):
        mixer = MixingAudioSource(music_volume=0.5, ducking_volume=0.2)
        mixer.play_input(AudioInput(AudioLane.MUSIC, FakePCM([pcm(10_000)] * 2), lambda _: None))
        mixer.play_input(AudioInput(AudioLane.EFFECT, FakePCM([pcm(1_000)]), lambda _: None))
        self.assertEqual(struct.unpack_from("<h", mixer.read())[0], 6_000)
        mixer.play_input(AudioInput(AudioLane.EFFECT, FakePCM([pcm(500)]), lambda _: None))
        mixer.play_input(AudioInput(AudioLane.SPEECH, FakePCM([pcm(1_000)]), lambda _: None))
        self.assertEqual(struct.unpack_from("<h", mixer.read())[0], 3_500)

    def test_lane_replacement_stop_completion_and_idle(self):
        completed = []
        first = FakePCM([pcm(1)])
        second = FakePCM([])
        mixer = MixingAudioSource()
        self.assertEqual(mixer.read(), bytes(PCM_FRAME_BYTES))
        mixer.play_input(AudioInput(AudioLane.EFFECT, first, completed.append))
        mixer.play_input(AudioInput(AudioLane.EFFECT, second, completed.append))
        self.assertTrue(first.cleaned)
        self.assertEqual(completed, [None])
        self.assertTrue(mixer.input_active(AudioLane.EFFECT))
        mixer.read()
        self.assertFalse(mixer.input_active(AudioLane.EFFECT))
        self.assertTrue(second.cleaned)
        mixer.stop_input(AudioLane.EFFECT)

    def test_source_error_cleanup_and_callback(self):
        errors = []
        source = FakePCM([], RuntimeError("decode failed"))
        mixer = MixingAudioSource()
        mixer.play_input(AudioInput(AudioLane.SPEECH, source, errors.append))
        mixer.read()
        self.assertTrue(source.cleaned)
        self.assertEqual(str(errors[0]), "decode failed")

    def test_levels_clipping_short_frames_and_cleanup(self):
        completed = []
        mixer = MixingAudioSource(music_volume=2, ducking_volume=-1)
        self.assertEqual((mixer.music_volume, mixer.ducking_volume), (1, 0))
        self.assertEqual(mixer.set_lane_volume(AudioLane.MUSIC, -2), 0)
        self.assertEqual(mixer.set_lane_volume(AudioLane.SPEECH, 0.5), 0.5)
        self.assertEqual(mixer.set_lane_volume(AudioLane.EFFECT, 3), 1)
        self.assertEqual(mixer.set_ducking_volume(3), 1)
        self.assertEqual((mixer.music_volume, mixer.ducking_volume), (0, 1))
        source = FakePCM([struct.pack("<h", 32_767)])
        mixer.play_input(AudioInput(AudioLane.SPEECH, source, completed.append))
        mixer.play_input(AudioInput(AudioLane.EFFECT, FakePCM([pcm(32_767)]), lambda _: None))
        self.assertEqual(struct.unpack_from("<h", mixer.read())[0], 32_767)
        mixer.cleanup()
        self.assertEqual(mixer.read(), b"")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            mixer.play_input(AudioInput(AudioLane.MUSIC, FakePCM([]), lambda _: None))
        self.assertFalse(mixer.is_opus())

    def test_audio_input_validation(self):
        valid = FakePCM([])
        cases = (
            (("music", valid, lambda _: None), "lane"),
            ((AudioLane.MUSIC, SimpleNamespace(cleanup=lambda: None), lambda _: None), "read"),
            ((AudioLane.MUSIC, SimpleNamespace(read=lambda: b""), lambda _: None), "cleanup"),
            ((AudioLane.MUSIC, valid, None), "callback"),
        )
        for arguments, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(TypeError, message):
                AudioInput(*arguments)


class DiscordAudioMixerTests(unittest.TestCase):
    def test_attach_reuse_replace_detach_and_busy_output(self):
        mixer = DiscordAudioMixer()
        first = Output()
        source = mixer.attach_output(first)
        self.assertIs(source, mixer.attach_output(first))
        second = Output(playing=True)
        with self.assertRaisesRegex(RuntimeError, "already playing"):
            mixer.attach_output(second)
        self.assertIs(mixer.connection, first)
        second.playing = False
        mixer.attach_output(second)
        mixer.detach_output()
        self.assertIsNone(mixer.connection)

    def test_routes_stops_and_sets_music_volume(self):
        mixer = DiscordAudioMixer(music_volume=0.5, ducking_volume=0.2)
        mixer.attach_output(Output())
        source = FakePCM([pcm(1)])
        mixer.play_input(AudioInput(AudioLane.MUSIC, source, lambda _: None))
        self.assertTrue(mixer.input_active(AudioLane.MUSIC))
        self.assertEqual(mixer.set_lane_volume(AudioLane.MUSIC, 2), 1)
        self.assertEqual(mixer.set_lane_volume(AudioLane.SPEECH, 0.7), 0.7)
        self.assertEqual(mixer.set_lane_volume(AudioLane.EFFECT, 0.8), 0.8)
        self.assertEqual(mixer.set_ducking_volume(0.4), 0.4)
        mixer.stop_input(AudioLane.MUSIC)
        self.assertTrue(source.cleaned)


if __name__ == "__main__":
    unittest.main()
