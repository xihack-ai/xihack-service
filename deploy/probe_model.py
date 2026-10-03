"""Direct official model probe: read key from stdin, print only safe metadata."""
import json
import sys
import time
import httpx

key = sys.stdin.read().strip()
if not key:
    raise SystemExit('Model credential is missing')
t = time.monotonic()
result = {'model': 'deepseek-flash', 'cache_used': False, 'fallback_used': False}
try:
    response = httpx.post('https://api.deepseek.com/chat/completions',
        headers={'Authorization': 'Bearer ' + key},
        json={'model': 'deepseek-flash', 'thinking': {'type': 'disabled'}, 'max_tokens': 64,
              'messages': [{'role': 'user', 'content': '只返回JSON对象 {"ok":true}'}]}, timeout=30)
    result['http_status'] = response.status_code
    if response.is_success:
        data = response.json()
        result['verified'] = json.loads(data['choices'][0]['message']['content']) == {'ok': True}
        result['usage'] = data.get('usage')
    else:
        result['verified'] = False
except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
    result.update(verified=False, error_type=type(exc).__name__)
result['elapsed_seconds'] = round(time.monotonic() - t, 2)
print(json.dumps(result))
raise SystemExit(0 if result['verified'] else 1)
