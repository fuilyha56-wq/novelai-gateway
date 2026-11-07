"""Readable QQ exports of the same OHLC data used by the browser charts."""
import io
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TZ = timezone(timedelta(hours=8))
GREEN, RED = '#089981', '#f23645'
INK, MUTED, GRID = '#182b42', '#586b82', '#e5ebf2'


def axis_ticks(low, high):
    span = max(high - low, high * .08, .1)
    rough = span * 1.24 / 5
    power = 10 ** math.floor(math.log10(rough))
    step = next(v for v in (1, 2, 2.5, 5, 10) if v * power >= rough) * power
    floor = max(0, math.floor((low - span * .12) / step) * step)
    ceiling = math.ceil((high + span * .12) / step) * step
    return [floor + i * step for i in range(round((ceiling - floor) / step) + 1)]


def number(value):
    return f'{value:.2f}'.rstrip('0').rstrip('.')


def render_latency_png(data):
    width, scale, card_height = 1280, 2, 480
    series = data['series']
    height = 214 + max(1, len(series)) * card_height + 112
    image = Image.new('RGB', (width * scale, height * scale), '#eff3f8')
    draw = ImageDraw.Draw(image)
    font_path = Path(__file__).parent / 'templates' / 'NotoSansSC.ttf'
    fonts = {}

    def font(size, bold=False):
        key = (size, bold)
        if key not in fonts:
            selected = ImageFont.truetype(str(font_path), size * scale)
            try:
                selected.set_variation_by_axes([600 if bold else 400])
            except OSError:
                pass
            fonts[key] = selected
        return fonts[key]

    def text(x, y, value, size=25, color=MUTED, bold=False, anchor='la'):
        draw.text((x * scale, y * scale), str(value), fill=color, font=font(size, bold), anchor=anchor)

    def line(x1, y1, x2, y2, color=GRID, weight=1, dashed=False):
        if dashed:
            length = max(abs(x2-x1), abs(y2-y1))
            for start in range(0, math.ceil(length), 10):
                end = min(length, start + 6)
                line(x1+(x2-x1)*start/length, y1+(y2-y1)*start/length,
                     x1+(x2-x1)*end/length, y1+(y2-y1)*end/length, color, weight)
        else:
            draw.line(tuple(v * scale for v in (x1, y1, x2, y2)), fill=color, width=weight*scale)

    def rect(box, fill, radius=0):
        draw.rounded_rectangle(tuple(v*scale for v in box), radius=radius*scale, fill=fill)

    queued = data['mode'] == 'queued'
    mode_label = '含排队 · 总耗时' if queued else '不排队 · 请求耗时'
    step = data['bucket_seconds']
    interval = f'{step / 3600:g} 小时' if step >= 3600 else f'{step / 60:g} 分钟' if step >= 60 else f'{step:g} 秒'
    stamp = lambda t: datetime.fromtimestamp(t, TZ)
    start, end = data['display_start'], data['display_end']
    text(36, 27, 'NAI 模型延迟', 44, INK, True)
    rect((910, 32, 1244, 91), '#e0e9f3', 12)
    text(1077, 43, mode_label, 27, INK, True, 'ma')
    text(38, 102, f"{stamp(data['window_start']):%m/%d %H:%M} — {stamp(data['window_end']):%m/%d %H:%M}  ·  北京时间 UTC+8", 26, INK)
    text(38, 151, f'每根 K 线 {interval}   ·   绿涨 / 红跌   ·   ' + ('虚线空心' if queued else '实心蜡烛'), 25)

    for row, item in enumerate(series):
        card_y = 214 + row * card_height
        rect((24, card_y, 1256, card_y + card_height - 16), 'white', 18)
        candles = [c for c in item['ohlc'] if c]
        latest = candles[-1]
        short = item['model'].replace('nai-diffusion-', 'NAI ').replace('4-5', '4.5').replace('-full', ' · Full').replace('-curated', ' · Curated').replace('-inpainting', ' · Inpaint').replace('-medium', ' · Medium')
        text(50, card_y + 18, short, 29, INK, True)
        text(1230, card_y + 14, f"{number(latest['close_ms']/1000)} s", 34, GREEN if latest['close_ms'] >= latest['open_ms'] else RED, True, 'ra')
        text(1230, card_y + 58, f"最新 {stamp(latest['last_timestamp']):%m/%d %H:%M:%S}", 21, MUTED, anchor='ra')
        text(50, card_y + 61, '延迟 / 秒', 22)
        left, right, top, bottom = 132, 1214, card_y + 112, card_y + 340
        ticks = axis_ticks(min(c['low_ms']/1000 for c in candles), max(c['high_ms']/1000 for c in candles))
        y = lambda v: bottom - (v - ticks[0]) / (ticks[-1] - ticks[0]) * (bottom-top)
        x = lambda t: left + (t-start)/(end-start)*(right-left)
        for tick in ticks:
            line(left, y(tick), right, y(tick))
            text(left - 18, y(tick), number(tick), 23, anchor='rm')
        # Natural boundary ticks; date and clock appear under EVERY model.
        stride = max(1, math.ceil(len(data['timestamps'])/6)) * step
        times = []
        t = math.ceil(start / stride) * stride
        while t <= end:
            if left + 105 <= x(t) <= right - 105:
                times.append(t)
            t += stride
        for t in times:
            px = x(t)
            line(px, top, px, bottom, '#f0f3f7')
            anchor = 'la' if t == start else 'ra' if t == end else 'ma'
            text(px, bottom+12, stamp(t).strftime('%H:%M:%S' if step < 60 else '%H:%M'), 23, INK, anchor=anchor)
            text(px, bottom+43, stamp(t).strftime('%m/%d'), 21, MUTED, anchor=anchor)
        slot = (right-left) / len(data['timestamps'])
        body_width = min(22, slot * .68)
        for i, candle in enumerate(item['ohlc']):
            if not candle:
                continue
            px = x(data['timestamps'][i] + step/2)
            o, h, l, c = (candle[k]/1000 for k in ('open_ms', 'high_ms', 'low_ms', 'close_ms'))
            color = GREEN if c >= o else RED
            line(px, y(h), px, y(l), color, 2, queued)
            a, b = sorted((y(o), y(c)))
            if b-a < 3:
                a, b = (a+b)/2-1.5, (a+b)/2+1.5
            if queued:
                rect((px-body_width/2, a, px+body_width/2, b), 'white')
                for x1,y1,x2,y2 in [(px-body_width/2,a,px+body_width/2,a),(px+body_width/2,a,px+body_width/2,b),(px+body_width/2,b,px-body_width/2,b),(px-body_width/2,b,px-body_width/2,a)]:
                    line(x1,y1,x2,y2,color,2,True)
            else:
                rect((px-body_width/2,a,px+body_width/2,b),color)
        legacy = sum(c.get('legacy_count',0) for c in candles)
        text(50, card_y+428, f"最低 {number(min(c['low_ms'] for c in candles)/1000)} s     最高 {number(max(c['high_ms'] for c in candles)/1000)} s", 22)
        text(1230, card_y+428, '含历史分钟均值' if legacy else f"{sum(c['count'] for c in candles)} 次请求", 22, anchor='ra')

    if not series:
        rect((24, 214, 1256, 658), 'white', 18)
        text(640, 355, '所选时段暂无请求记录', 34, INK, True, 'ma')
        text(640, 415, '新请求完成后会自动出现在图中', 25, anchor='ma')
    foot = height - 100
    text(38, foot, '实体：时段首末延迟   ·   影线：时段最高 / 最低   ·   空白：该时段无记录', 24, INK)
    text(38, foot+40, '绿涨代表延迟增加，红跌代表延迟降低；旧数据由分钟均值汇总。', 23)
    output = io.BytesIO()
    image.resize((width,height), Image.Resampling.LANCZOS).save(output, format='PNG')
    return output.getvalue()
