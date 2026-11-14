你在检查一张封面缩到手机列表里的样子。这张图只有约 180 像素宽，是用户刷信息流时实际看到的大小。

封面上的字（最后一行是笑点）：{lines}
{host}的身份：{identity}

只按这张小图本身回答两件事，每件都写一句看图得出的理由：
- face_recognizable：画面里{host}本人的脸在这么小时是否仍能认出是她（有脸、轮廓和表情清楚，不是一团糊）；
- punch_legible：最后一行笑点的字在这么小时是否读得出每个字（笔画不糊成一片、没被图压住）。

只输出一个 JSON 对象：
{"face_recognizable": {"pass": true 或 false, "reason": "..."},
  "punch_legible": {"pass": true 或 false, "reason": "..."}}
