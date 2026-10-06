import json, urllib.request, urllib.error

API = 'http://47.116.22.227:3000/v1/chat/completions'
KEY = 'sk-BQGQ3Tj73Q3TMVjlHtPOLFNdoqmHiCewgUgLSIH0JQt7G1BB'
headers = {
    'Authorization': f'Bearer {KEY}',
    'Content-Type': 'application/json',
}

def call(prompt, max_tokens=256, temp=0):
    body = {
        'model': 'deepseek-v4.1-flash',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': temp,
        'max_tokens': max_tokens,
    }
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
            msg = data['choices'][0]['message']['content']
            finish = data['choices'][0].get('finish_reason')
            usage = data.get('usage')
            return {'ok': True, 'text': msg, 'finish': finish, 'usage': usage, 'model': data.get('model')}
    except Exception as e:
        try:
            body_text = e.read().decode('utf-8', 'replace')
        except Exception:
            body_text = str(e)
        return {'ok': False, 'error': str(e), 'body': body_text}

print('=== CONTEXT CHECK ===')
for n in [10, 50, 200, 1000, 5000, 20000, 50000, 100000, 200000, 400000]:
    prompt = ('the ' * n)
    res = call(prompt, max_tokens=64)
    if res['ok']:
        text = res['text'].replace('\n', ' ')[:120]
        print(f'N={n:>6} OK finish={res["finish"]} text={text!r} usage={res["usage"]}')
    else:
        print(f'N={n:>6} ERROR {res["error"]} body={res["body"][:300]}')
        break

print('\n=== OUTPUT CHECK ===')
for m in [64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768]:
    prompt = 'Write exactly the word "the" once and then stop.'
    res = call(prompt, max_tokens=m)
    if res['ok']:
        text = res['text'].replace('\n', ' ')[:180]
        print(f'max_tokens={m:>5} OK finish={res["finish"]} text={text!r} usage={res["usage"]}')
    else:
        print(f'max_tokens={m:>5} ERROR {res["error"]} body={res["body"][:300]}')
        break

print('\n=== THE-WORD TRIGGER CHECK ===')
for n in [100, 500, 1000, 2000, 5000, 10000, 20000, 50000]:
    prompt = ' '.join(['the'] * n)
    res = call(prompt, max_tokens=256)
    if res['ok']:
        text = res['text'].replace('\n', ' ')[:160]
        print(f'the_n={n:>5} OK text={text!r}')
    else:
        print(f'the_n={n:>5} ERROR {res["error"]} body={res["body"][:300]}')
        break
