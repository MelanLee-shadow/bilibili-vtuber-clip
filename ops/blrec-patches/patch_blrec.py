from pathlib import Path
p = Path('/usr/local/lib/python3.10/site-packages/blrec/bili/danmaku_client.py')
text = p.read_text()
old = "self._api_platform: ApiPlatform = 'web'"
new = "self._api_platform: ApiPlatform = 'android'"
if old in text:
    text = text.replace(old, new)
    p.write_text(text)
    print('patched blrec danmaku api platform to android')
elif new in text:
    print('blrec danmaku api platform already android')
else:
    raise SystemExit('failed to locate danmaku api platform assignment')

text = p.read_text()
old = '"uid": self._uid,'
new = '"uid": 0,'
if old in text:
    text = text.replace(old, new)
    p.write_text(text)
    print('patched blrec danmaku websocket auth uid to anonymous')
elif new in text:
    print('blrec danmaku websocket auth uid already anonymous')
else:
    raise SystemExit('failed to locate danmaku websocket auth uid assignment')
