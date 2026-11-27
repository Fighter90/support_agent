"""Озвучка видео: синтез речи macOS (голос Milena, ru_RU) → out/<id>.wav и out/durations.json."""

import json
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / 'out'
OUT.mkdir(exist_ok=True)
VOICE, RATE = 'Milena', '178'

dur = {}
for item in json.loads((HERE / 'narration.json').read_text(encoding='utf-8')):
    aiff, wav = OUT / f'{item["id"]}.aiff', OUT / f'{item["id"]}.wav'
    subprocess.run(['say', '-v', VOICE, '-r', RATE, '-o', str(aiff), item['text']], check=True)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(aiff), '-ar', '48000', '-ac', '1', str(wav)], check=True)
    d = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(wav)],
                       capture_output=True, text=True, check=True).stdout
    dur[item['id']] = round(float(d), 2)
    aiff.unlink()
(OUT / 'durations.json').write_text(json.dumps(dur, ensure_ascii=False, indent=1), encoding='utf-8')
print(dur, 'итого', round(sum(dur.values()), 1), 'с')
