"""Сборка ролика из одного дубля: ускорение только отрезков ожидания ответа модели, озвучка и субтитры.

Вход: out/raw.webm, out/timeline.json, out/<id>.wav, narration.json.  Выход: out/demo.mp4 и out/demo.srt.
Монтажа нет: все кадры записи идут подряд, меняется только скорость отрезков ожидания (разрешено регламентом).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / 'out'
LIMIT = 178.0  # регламент: до 3 минут

tl = json.loads((OUT / 'timeline.json').read_text())
texts = {i['id']: i.get('sub', i['text']) for i in json.loads((HERE / 'narration.json').read_text(encoding='utf-8'))}
dur = json.loads((OUT / 'durations.json').read_text())
raw_len = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(OUT / 'raw.webm')],
                               capture_output=True, text=True).stdout or tl['end'] / 1000)
lag = 0.0  # запись видео начинается вместе с отсчётом сценария (запуск браузера); хвост записи — после финала
waits = [(w['start'] / 1000 + lag, w['end'] / 1000 + lag) for w in tl['waits'] if w['end'] - w['start'] > 1500]
total_wait = sum(b - a for a, b in waits)


def out_len(speed: float) -> float:
    return raw_len - total_wait + total_wait / speed


speed = 4.0
while out_len(speed) > LIMIT and speed < 16:
    speed += 1
print(f'запись {raw_len:.1f} с, ожидание модели {total_wait:.1f} с, ускорение ×{speed:g} → ролик {out_len(speed):.1f} с')

segs, cur = [], 0.0
for a, b in waits:
    if a > cur:
        segs.append((cur, a, 1.0))
    segs.append((a, b, speed))
    cur = b
segs.append((cur, raw_len, 1.0))


def f(t: float) -> float:  # время записи → время ролика
    o = 0.0
    for a, b, s in segs:
        if t <= a:
            break
        o += (min(t, b) - a) / s
    return o


def ts(x: float) -> str:
    ms = int(round(x * 1000))
    return f'{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}'


cues = []
for n in tl['narration']:
    start, d, text = f(n['t'] / 1000 + lag), dur[n['id']], texts[n['id']]
    parts = []
    for s in re.split(r'(?<=[.!?])\s+', text):
        while len(s) > 84:
            cut = s.rfind(',', 0, 84)
            cut = cut if cut > 30 else s.rfind(' ', 0, 84)
            parts.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        parts.append(s)
    total = sum(len(p) for p in parts)
    t = start
    for p in parts:
        dt = d * len(p) / total
        cues.append((t, t + dt - 0.05, p))
        t += dt
for a, b in waits:
    if f(b) - f(a) > 0.8:
        cues.append((f(a), f(b), f'[ожидание ответа модели ускорено ×{speed:g}]'))
cues.sort()
srt = '\n'.join(f'{i}\n{ts(a)} --> {ts(b)}\n{t}\n' for i, (a, b, t) in enumerate(cues, 1))
(OUT / 'demo.srt').write_text(srt, encoding='utf-8')

vf = ''.join(f'[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{s:g}[v{i}];' for i, (a, b, s) in enumerate(segs))
vf += ''.join(f'[v{i}]' for i in range(len(segs))) + f'concat=n={len(segs)}:v=1:a=0[vc];'
style = "FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H99000000,BorderStyle=4,Outline=1,Shadow=0,MarginV=28"
vf += '[vc]null[v]'  # субтитры уже в кадре (записаны в одном дубле); .srt добавляется отдельной дорожкой
inputs = ['-i', str(OUT / 'raw.webm')]
af = ''
for k, n in enumerate(tl['narration'], 1):
    inputs += ['-i', str(OUT / f'{n["id"]}.wav')]  # noqa
    ms = int(f(n['t'] / 1000 + lag) * 1000)
    af += f'[{k}:a]adelay={ms}|{ms}[a{k}];'
af += ''.join(f'[a{k}]' for k in range(1, len(tl['narration']) + 1)) + f'amix=inputs={len(tl["narration"])}:normalize=0,apad[a]'
srt_idx = len(tl['narration']) + 1
inputs += ['-i', str(OUT / 'demo.srt')]
cmd = ['ffmpeg', '-y', '-loglevel', 'error', *inputs, '-filter_complex', vf + ';' + af, '-map', '[v]', '-map', '[a]', '-map', f'{srt_idx}:s',
       '-c:s', 'mov_text', '-metadata:s:s:0', 'language=rus',
       '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p', '-r', '25',
       '-c:a', 'aac', '-b:a', '160k', '-t', f'{out_len(speed):.2f}', '-movflags', '+faststart', str(OUT / 'demo.mp4')]
subprocess.run(cmd, check=True)
print('OK', OUT / 'demo.mp4', sys.argv[1:] or '')
