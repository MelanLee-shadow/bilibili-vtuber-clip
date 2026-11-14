你在审一张直播切片封面候选（第一张图，1920x1080）。第二张图是这条切片的源画面（同一时刻的直播截图），用来核对人物身份。

故事：{title}
封面字：{lines}
这段故事的参与者（原样抄进 people.expected）：{participants}
{host}的身份：{identity}
这张候选的做法：{route}（{route_note}）

逐项看图判断，每项都写一句看图得出的理由：
- people：画面上实际能认出的参与者（只能从参与者名单里选；表情包、Q 版、抠图都算出现）；
- hook_fit：画面是否表现出封面字那一刻的情绪或场景，2 = 表现出来了，1 = 不冲突但没表现，0 = 拧着；
- clean_layers：贴上去的每个图层，在画布之内看得见的部分是否干净。人物或物件被画布边缘（上、下、左、右任一边）裁掉不算缺陷，封面有意让人物放大后下半身或一侧出画；算缺陷的只有画布之内看得见的问题：图层自带的平直切边且落在画布内部、残留背景、抠图破洞、方块底、边缘破损；
- identity：画面里的人物是否和源画面里的本人一致（发型、耳朵、发饰、服装；不能多出帽子、饰品），表情没有吐舌头；
- source_faithful：画面是否忠于源画面的人物与事件（没有把别人画成没出现过的样子）；
- no_fabrication：画面是否没有编造字幕里没发生的事或人物；
- face：{host}本人主脸（眉毛到下巴、左脸颊到右脸颊）在这张候选里的像素框 [x0,y0,x1,y1]，整数；
- keep：文字不应压住的其他要紧区域（另一位参与者的脸等）的像素框列表，整数；没有就写空数组。

只输出一个 JSON 对象：
{"people": {"expected": [与上面参与者名单逐字相同、顺序相同], "present": [...], "reason": "..."},
  "hook_fit": {"score": 0、1 或 2, "reason": "..."},
  "clean_layers": {"pass": true 或 false, "reason": "..."},
  "identity": {"pass": true 或 false, "reason": "..."},
  "source_faithful": {"pass": true 或 false, "reason": "..."},
  "no_fabrication": {"pass": true 或 false, "reason": "..."},
  "face": [x0, y0, x1, y1],
  "keep": [[x0, y0, x1, y1]]}
