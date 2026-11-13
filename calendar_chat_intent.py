"""Conservative field scopes for proposals, independent from calendar defaults.

This is not a general NL parser. It prevents an LLM's full-Event completion from
silently becoming a patch: optional fields need explicit user intent signals.
Unknown phrasing is clarified rather than applied to unrelated fields.
"""
import re


def requested_fields(message, creating=False):
    text = message.casefold()
    fields = {'title', 'date'} if creating else set()
    rename = re.search(r'改名(?:为|叫|成)?|重命名(?:为|成)?|改叫|改成|改为|rename', text)
    if rename and not creating:
        tail = text[rename.end():].strip(' ：:为')
        first, *rest = re.split(r'[，,；;]|同时|并且|另外|然后', tail, maxsplit=1)
        # Names can themselves contain “提醒/颜色/持续三天”. Do not treat those
        # words as extra instructions in a rename. Explicit time/type values
        # after generic 改为/改成 remain ordinary field changes.
        generic_value = re.fullmatch(r'紧急|一般|普通|ddl|urgent|general|(?:上午|下午|晚上)?[\d一二三四五六七八九十两]+点(?:半)?|\d{1,2}:\d{2}|明天|后天|今天|\d{1,2}[./-]\d{1,2}|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日号]?', first.strip())
        if rename.group() not in ('改成', '改为') or not generic_value:
            fields.add('title')
            text = rest[0] if rest else ''
    signals = {
        'date': r'日期|今天|明天|后天|昨天|周[一二三四五六日天末]|星期|[下本这]周|\d{1,2}[./-]\d{1,2}|\d{1,2}月|date',
        'time': r'时间|几点|(?:\d{1,2}|[零〇一二三四五六七八九十两]+)[点时]|\d{1,2}[:：]\d{2}|全天|无具体时间|time',
        'event_type': r'ddl|截止|紧急|一般|普通事项|urgent|general|事项性质',
        'reminder': r'提醒|提前.{0,8}(?:分钟|小时)|remind',
        'duration_days': r'持续|连续|为期|天数|(?:\d+|[一二三四五六七八九十]+)(?:个)?(?:工作日|天)|duration',
        'skip_non_working_days': r'跳过|避开|仅工作日|只.{0,3}工作日|不含周末|skip',
        'end_as_ddl': r'最后一天|末日|最后.{0,3}(?:截止|ddl)|end.as.ddl',
        'category_id': r'分类|归类|归入|归到|category',
        'color': r'颜色|色条|配色|#[0-9a-f]{6}|color',
        'color_mode': r'跟随.{0,4}(?:分类|颜色)|继承.{0,4}颜色|独立颜色|单独颜色|颜色来源|color.mode',
        'notes': r'备注|补充说明|附注|notes?',
    }
    for key, pattern in signals.items():
        if re.search(pattern, text):
            fields.add(key)
    if re.search(r'改名|重命名|改叫|标题|名称|rename|title|改[成为]', text):
        fields.add('title')
    return fields


def scope_patch(arguments, message, creating=False, continuation=()):
    allowed = requested_fields(message, creating) | set(continuation)
    kept = {key: value for key, value in arguments.items() if key in allowed}
    ignored = sorted(set(arguments)-set(kept))
    return kept, ignored
