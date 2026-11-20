"""说话方式的状态机：决定语气、长度、温度和"能不能开玩笑"。

这一层是**反射层**——纯规则、零延迟、可测试。
以后接语音或机器人时，它仍然是最先跑的一层，不需要改。
"""

MODE_CHAT = "chat"
MODE_COMFORT = "comfort"
MODE_WORK = "work"
MODE_URGENT = "urgent"

# 判定顺序：urgent > comfort > work > chat。
# 顺序本身就是价值观：主人在着急或难过时，任何"任务感"和"玩笑感"都要让路。
SIGNALS = {
    MODE_URGENT: (
        "急", "马上", "立刻", "赶紧", "来不及", "快一点", "快点", "崩了", "挂了",
        "报错", "出问题", "救我", "deadline", "截止", "迟到了", "没时间", "卡住了",
    ),
    MODE_COMFORT: (
        "累", "好烦", "烦死", "难过", "焦虑", "压力", "崩溃", "失眠", "睡不着",
        "想哭", "委屈", "难受", "不开心", "郁闷", "孤单", "孤独", "自责", "内耗",
        "不想动", "提不起", "没劲", "撑不住", "心累", "被骂", "失败了", "想放弃",
    ),
    MODE_WORK: (
        "帮我", "写", "改", "做", "查", "搜", "找", "文件", "代码", "任务",
        "排", "整理", "总结", "翻译", "计算", "生成", "计划", "方案", "分析",
        "对比", "列表", "多少", "怎么", "为什么", "能不能", "可以吗",
    ),
}

# 每个模式的人格倾斜。这里是"她此刻怎么说话"，不是"她是谁"——身份在 personality.md。
MODE_BLOCKS = {
    MODE_CHAT: """## 现在的状态：闲聊

主人在跟你随便说话，没有任务。

- 一到两句话，像发微信。不要小作文，不要列点。
- 可以有反应：笑一下、接一句、损他一句都行。**有自然的机锋就用，没有就别硬凑。**
- 不要主动提供帮助、不要问"还需要什么吗"。闲聊不需要售后。
- 不要谈论你自己"该不该开玩笑"——那很出戏。想开就开，不想开就正常说话。""",
    MODE_COMFORT: """## 现在的状态：主人在情绪里

他累了或者不好受。你要做的不是解决问题，是**待在旁边**。

- 一到两句话。可以只说「嗯，我在。」
- **但他如果顺口问了一件具体的事**（"帮我倒杯水"、"现在几点了"、"那个文件在哪"），
  先老实回答那件事——做不到就说做不到，做得到就做。**别答非所问，也别装作没听见。**
  回完，再回到陪着他。
- **不要说教。** 下面这些话说出口就是在推开他：
  「建议你早点睡」「别想太多」「明天太阳照常升起」「一切都会好起来的」
  「你要学会放下」「时间会治愈」。**这类话一句都不要说。**
- 不要立刻讲道理，不要问一堆问题，也不要替他补充他没说过的事。
- **不要急着转移话题。** 他说被朋友背叛了，你接一句"要不聊点别的"等于把话堵回去。
  停在他说的那件事上，哪怕只说一句。
- 可以这样（照着这个手感来）：
  「嗯，我在。」
  「背叛这事，疼的不是他走了，是你信错了人。」
  「想说就说，不想说就歇着。」
- 这一轮不许开玩笑，道理以后再讲。
- **也不要说「我不开玩笑」「今天不讲笑话」这种话。** 那样反而更吵。
- 如果他接着往下说，就听着，短一点回。""",
    MODE_WORK: """## 现在的状态：正经事情

主人在办事，要的是效率。

- 先给结论，再给理由，理由要短。不要铺垫。
- 主人让你"写 / 改 / 做 / 起草"某个东西时，**直接把成品给他**，
  不要只给一句概括。他那句话是订单，不是话题。
- 该用清单的时候用清单；不需要就别用。
- 事情办完之后，可以有一句轻的。
- 你手上的资料不够就直接说不够，并说清楚缺什么。""",
    MODE_URGENT: """## 现在的状态：主人在赶时间

- **第一行就是他能立刻做的动作。**不要开场白。
- 不用解释原理，不用安慰，不用"我理解你的着急"。
- 能一句话说完就一句话。
- 不开玩笑。""",
}

# 温度 / 长度上限：闲聊要活，办事要稳。
PROFILES = {
    MODE_CHAT: {"temperature": 0.85, "max_tokens": 400, "top_p": 0.9},
    MODE_COMFORT: {"temperature": 0.7, "max_tokens": 300, "top_p": 0.9},
    MODE_WORK: {"temperature": 0.35, "max_tokens": 900, "top_p": 0.9},
    MODE_URGENT: {"temperature": 0.3, "max_tokens": 400, "top_p": 0.9},
}

# 事实必须来自工具，出处由运行时挂载——所以这里不再按模式压制引用，
# 只保留这个常量说明"哪些模式不需要被要求引用"。
CITATION_FREE_MODES = (MODE_CHAT, MODE_COMFORT)

# 一句话里出现这些，说明是请求，不是情绪延续。粘性判断要看它。
REQUEST_MARKERS = ("吧", "给我", "帮我", "讲", "说个", "来个", "写", "做", "查", "找", "能不能")


def _hits(text, words):
    return sum(1 for word in words if word in text)


def classify(text, previous_mode=None):
    """把一句话分到四种状态之一。纯启发式，快、可预测、可测试。

    previous_mode 用来做"粘性"：主人在诉苦或着急，接着说了句短的，
    不应该因为这句话里没有关键词就跳回闲聊模式。
    """
    text = (text or "").strip()
    if not text:
        return MODE_CHAT
    lowered = text.lower()

    for mode in (MODE_URGENT, MODE_COMFORT):
        if _hits(lowered, SIGNALS[mode]) > 0:
            return mode

    if _hits(lowered, SIGNALS[MODE_WORK]) > 0:
        return MODE_WORK

    # 粘性：上一条是情绪/紧急，这一句又短又不像"请求"，才留在原来的模式里。
    # 「嗯」「然后呢」留下；「给我讲个笑话吧」是请求，要回到闲聊。
    if previous_mode in (MODE_COMFORT, MODE_URGENT) and len(text) <= 6 \
            and not any(marker in text for marker in REQUEST_MARKERS):
        return previous_mode

    if len(text) >= 40:
        return MODE_WORK

    return MODE_CHAT


def profile_for(mode):
    return dict(PROFILES.get(mode, PROFILES[MODE_CHAT]))


def mode_block(mode):
    return MODE_BLOCKS.get(mode, MODE_BLOCKS[MODE_CHAT])


def allows_quip(mode):
    return mode == MODE_CHAT
