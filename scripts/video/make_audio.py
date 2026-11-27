"""Озвучка видео нейросетевым синтезом речи Piper (VITS, русские голоса rhasspy/piper-voices, локально, ONNX).

Живая подача: разговорный текст (narration.json), чуть разная вариативность интонации (noise_scale), естественный
темп (length_scale), паузы между предложениями, лёгкая обработка (выравнивание громкости, мягкий фильтр шипящих).
Окружение: uv venv -p 3.12 ~/.venvs/tts && uv pip install -p ~/.venvs/tts piper-tts numpy soundfile
Голоса: ~/.cache/piper/ru_RU-<голос>-medium.onnx (+ .onnx.json) с huggingface.co/rhasspy/piper-voices
Запуск: ~/.venvs/tts/bin/python make_audio.py [голос]   (dmitri, irina, ruslan; по умолчанию dmitri)
Выход: out/<id>.wav (48 кГц) и out/durations.json; out/_samples/ — образцы всех голосов для сравнения.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from piper import PiperVoice, SynthesisConfig

HERE = Path(__file__).parent
OUT = HERE / 'out'
OUT.mkdir(exist_ok=True)
VOICES = Path.home() / '.cache' / 'piper'
SPEAKER = sys.argv[1] if len(sys.argv) > 1 else 'dmitri'
CFG = SynthesisConfig(length_scale=1.04, noise_scale=0.72, noise_w_scale=0.85)


def speak(voice: PiperVoice, text: str) -> tuple[np.ndarray, int]:
    sr, parts = voice.config.sample_rate, []
    for sent in re.split(r'(?<=[.!?])\s+', text.strip()):
        chunks = [c.audio_float_array for c in voice.synthesize(sent, CFG)]
        parts.append(np.concatenate(chunks))
        parts.append(np.zeros(int(sr * 0.32), dtype=np.float32))  # пауза между предложениями, как у живого диктора
    return np.concatenate(parts[:-1]), sr


def finish(raw: Path, out: Path) -> float:
    # 48 кГц, срез низкого гула, мягкое приглушение резких верхов, выравнивание громкости
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(raw), '-af',
                    'highpass=f=70,equalizer=f=6500:t=q:w=1.2:g=-3,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120,'
                    'loudnorm=I=-16:TP=-1.5:LRA=7,aresample=48000,adelay=120|120,apad=pad_dur=0.12', '-ac', '1', str(out)], check=True)
    d = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(out)],
                       capture_output=True, text=True, check=True).stdout
    return round(float(d), 2)


def main() -> None:
    items = json.loads((HERE / 'narration.json').read_text(encoding='utf-8'))
    if '--samples' in sys.argv:  # образец каждого голоса на первой фразе
        (OUT / '_samples').mkdir(exist_ok=True)
        for onnx in sorted(VOICES.glob('ru_RU-*-medium.onnx')):
            v = PiperVoice.load(str(onnx))
            a, sr = speak(v, items[0]['text'])
            sf.write(OUT / '_samples' / f'{onnx.stem}.wav', a, sr)
        return
    voice = PiperVoice.load(str(VOICES / f'ru_RU-{SPEAKER}-medium.onnx'))
    dur = {}
    for it in items:
        a, sr = speak(voice, it['text'])
        raw = OUT / f'{it["id"]}.raw.wav'
        sf.write(raw, a, sr)
        dur[it['id']] = finish(raw, OUT / f'{it["id"]}.wav')
        raw.unlink()
    (OUT / 'durations.json').write_text(json.dumps(dur, ensure_ascii=False, indent=1), encoding='utf-8')
    print(SPEAKER, dur, 'итого', round(sum(dur.values()), 1), 'с')


if __name__ == '__main__':
    main()
