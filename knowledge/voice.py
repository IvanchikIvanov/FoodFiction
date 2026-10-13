"""Local audio transcription. Language detection never uses company facts."""
import asyncio
import io
from pathlib import Path

from .languages import LANGUAGES

MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 45
VOICE_LANGUAGES = {code.split("-")[0]: code for code in LANGUAGES if code != "ky-KG"}


class VoiceError(Exception):
    pass


def decode_audio(data):
    import av
    import numpy as np
    parts, length = [], 0
    try:
        with av.open(io.BytesIO(data)) as audio:
            if not audio.streams.audio:
                raise VoiceError("В записи нет звука. Попробуйте ещё раз.")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
            for frame in audio.decode(audio=0):
                for resampled in resampler.resample(frame):
                    samples = resampled.to_ndarray().flatten()
                    length += len(samples)
                    if length > MAX_AUDIO_SECONDS * 16000:
                        raise VoiceError("Вопрос слишком длинный. Запишите вопрос короче 45 секунд.")
                    parts.append(samples)
            for resampled in resampler.resample(None):
                samples = resampled.to_ndarray().flatten()
                length += len(samples)
                if length > MAX_AUDIO_SECONDS * 16000:
                    raise VoiceError("Вопрос слишком длинный. Запишите вопрос короче 45 секунд.")
                parts.append(samples)
    except VoiceError:
        raise
    except Exception:
        raise VoiceError("Не удалось прочитать запись. Попробуйте ещё раз.") from None
    if length < 8000:
        raise VoiceError("Запись слишком короткая. Произнесите вопрос целиком.")
    samples = np.concatenate(parts)
    if float(np.sqrt(np.mean(samples ** 2))) < 0.002:
        raise VoiceError("Речь не слышна. Поднесите микрофон ближе и повторите вопрос.")
    return samples


class VoiceTranscriber:
    def __init__(self, model_path):
        self.model_path = model_path
        self.model = None
        self.lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(self.model_path and (Path(self.model_path) / "model.bin").is_file())

    def recognize(self, data):
        from faster_whisper import WhisperModel
        samples = decode_audio(data)
        if self.model is None:
            self.model = WhisperModel(self.model_path, device="cpu", compute_type="int8",
                                      cpu_threads=8, num_workers=1, local_files_only=True)
        segments, info = self.model.transcribe(samples, task="transcribe", language=None,
            vad_filter=True, beam_size=3, condition_on_previous_text=False, temperature=0)
        transcript = " ".join(segment.text.strip() for segment in segments
                              if segment.no_speech_prob < 0.6 and segment.avg_logprob > -1.0).strip()
        if not transcript:
            raise VoiceError("Не удалось разобрать вопрос. Говорите чуть громче и повторите запись.")
        if len(transcript) > 1000:
            raise VoiceError("Вопрос слишком длинный. Сформулируйте его короче.")
        if info.language not in VOICE_LANGUAGES or info.language_probability < 0.35:
            raise VoiceError("Не удалось уверенно определить поддерживаемый язык. Повторите вопрос целиком.")
        return {"query": transcript, "language": VOICE_LANGUAGES[info.language],
                "language_probability": round(info.language_probability, 3)}

    async def transcribe(self, data):
        if not self.enabled:
            raise VoiceError("Распознавание речи пока недоступно. Попробуйте позже.")
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=1)
        except TimeoutError:
            raise VoiceError("Распознавание занято. Повторите вопрос через несколько секунд.") from None

        async def work():
            try:
                return await asyncio.to_thread(self.recognize, data)
            except VoiceError:
                raise
            except Exception:
                raise VoiceError("Не удалось распознать запись. Попробуйте ещё раз.") from None
            finally:
                self.lock.release()

        # A disconnected browser must not free the slot while the model still runs.
        task = asyncio.create_task(work())
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=180)
        except TimeoutError:
            raise VoiceError("Распознавание занимает слишком долго. Попробуйте короткий вопрос позже.") from None
