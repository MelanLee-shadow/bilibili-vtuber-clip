# 封面画面说明（第 6 步，给出图用）

这是一条直播切片的最终字幕（时间、文字），标题和封面字已经定好。封面会列几种做法（截图、官方表情包、模型抠图、
这一刻的 Q 版贴纸、整幅场景），按成品打分选一种。请根据最终字幕写出图需要的说明。每一项都要在字幕里有依据，
不编造没发生的事；字幕里没有的就写空字符串。

标题：{title}
封面字：{lines}
参与者：{participants}
{compilation_context}
最终字幕：
{context}

只输出一个 JSON 对象：
{"quotes": ["封面字对应的那一两句原话，逐字抄自字幕"],
  "expression": "英文：封面那一刻李豆沙的表情，写具体（不写 cute），不吐舌头",
  "action": "英文：她说那句话时的动作",
  "scene_needed": true 或 false,
  "scene_reason": "只有故事必须交代一个具体场景（动作、物件、状态关系）才写 true，并写出理由；聊天、反应写 false",
  "setting": "英文：scene_needed 为 true 时的场景；否则空字符串",
  "kmx": true 或 false（这段的笑点是否落在粉丝 kmx 身上）}
