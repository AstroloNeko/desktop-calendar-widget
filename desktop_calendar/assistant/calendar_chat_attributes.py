"""AI proposal adaptation; all actual date expansion remains in Store."""
import re
from datetime import date

from calendar_core import COLORS
from desktop_calendar.assistant.calendar_chat_tools import fail, reference_today
from desktop_calendar.assistant.calendar_chat_intent import requested_fields, scope_patch


def adapt_attributes(values, message, creating, categories, continuation=()):
    # Notes are data, not a second set of commands.
    instruction = re.split(r'备注(?:是|为|[:：])?', message, maxsplit=1)[0]
    scoped, ignored = scope_patch(values, instruction, creating, continuation)
    explicit = requested_fields(instruction, creating) | set(continuation)
    # Only a legacy creation can infer the year from its first month/day.
    # In a patch that date can belong to the old target rather than the new value.
    md = re.search(r'(?<![\d-])(\d{1,2})(?:[./]|月)(\d{1,2})(?:日|号)?', instruction)
    if creating and 'date' in scoped and md and not re.search(r'\d{4}[-年/.]|明年|去年|下个月|上个月', instruction):
        today = reference_today(date.today)
        try:
            expected = date(today.year, int(md[1]), int(md[2]))
        except ValueError:
            fail('指定的月日不存在，请明确日期。', 'clarification_required')
        if expected < today:
            fail('该月日在今年已过去，请明确年份或确认是否补记过去事项。', 'clarification_required')
        scoped['date'] = expected.isoformat()
    basis = values.get('category_basis')
    inferred = []
    if creating and basis == 'semantic_high' and 'category_id' not in explicit:
        identifier = values.get('category_id')
        if identifier and any(c['id'] == identifier for c in categories):
            scoped['category_id'] = identifier
            inferred.append('category_id')
    if 'category_id' in explicit and re.search(r'无分类|不分类|不归类', instruction):
        scoped['category_id'] = None
        inferred = []
    else:
        # Explicit names override a model's inferred ID. Similar names must not
        # silently select the first category.
        names = [c for c in categories if 'category_id' in explicit and c['name'] in instruction and
                 re.search(re.escape(c['name'])+r'(?:分类|类)|(?:归到|归入|分类[为是：:]?)\s*'+re.escape(c['name']), instruction)]
        if len(names) > 1:
            fail('匹配到多个分类，请明确指定分类名称。', 'clarification_required')
        named = re.search(r'(?:归到|归入|放到|分类[为是：:])\s*([^，,。；;]+?)(?:分类|类)(?:$|[，,。；;])', instruction)
        if named and named[1].strip() not in {c['name'] for c in categories}:
            fail('没有找到指定分类，请选择已有分类；不会自动新建分类。', 'clarification_required')
        if names:
            scoped['category_id'] = names[0]['id']
            inferred = []
    # Resolve color names using the current GUI palette, never model HEX guesses.
    colors = [value for name, value in COLORS.items() if name in instruction or
              any(token+'色' in instruction for token in name if token in '红黄绿蓝紫橙')]
    if 'color' in explicit and len(set(colors)) > 1:
        fail('提到了多种颜色，请明确本事项使用哪一种。', 'clarification_required')
    if colors and 'color' in explicit:
        scoped.update(color=colors[0], color_mode='override')
    if 'color_mode' in explicit and re.search(r'跟随.*分类|继承.*颜色|颜色跟.*分类|跟.*分类颜色', instruction):
        if creating and not scoped.get('category_id'):
            fail('请先明确需要跟随哪个分类的颜色。', 'clarification_required')
        scoped.pop('color', None)
        scoped['color_mode'] = 'inherit'
    elif 'color' in explicit and '默认颜色' in instruction:
        scoped.update(color=next(iter(COLORS.values())), color_mode='override')
    if 'notes' in values and '备注' in message:
        scoped['notes'] = values['notes']
    if re.search(r'(?:到|至|做到).*(?:周|星期|\d.*[日号])', instruction) and 'end_date' in values:
        scoped['end_date'] = values['end_date']
    scoped.pop('category_basis', None)
    return scoped, sorted(set(values)-set(scoped)-{'category_basis'}), dict(
        explicit=sorted(set(scoped)-set(inferred)), inferred=inferred,
        defaulted='remaining fields supplied by Store', ambiguous=[])
