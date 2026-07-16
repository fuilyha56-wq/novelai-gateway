import os
import socket

try:
    import httpx
except Exception as e:
    print('HTTPX_IMPORT_ERROR', e)
    raise

print('SHARED_API_KEY=', repr(os.getenv('SHARED_API_KEY')))
print('https_proxy=', repr(os.getenv('https_proxy')))
print('http_proxy=', repr(os.getenv('http_proxy')))
print('all_proxy=', repr(os.getenv('all_proxy')))
print('PYTHON_VERSION=', repr(os.sys.version))
try:
    infos = socket.getaddrinfo('image.novelai.net', 443)
    print('DNS_COUNT=', len(infos))
    print('DNS_FIRST=', infos[0])
except Exception as e:
    print('DNS_ERR=', type(e).__name__, e)

try:
    with httpx.Client(timeout=15.0, trust_env=True) as client:
        resp = client.get('https://image.novelai.net/')
        print('HTTP_STATUS=', resp.status_code)
        print('HTTP_HEADERS=', {k:v for k,v in resp.headers.items() if k.lower() in ['content-type','server','date']})
        print('HTTP_TEXT_PREFIX=', resp.text[:200])
except Exception as e:
    print('HTTP_ERR=', type(e).__name__, e)
    raise
