"""Closed tool contract. No dynamic method dispatch or permissive Event coercion."""
import re
from datetime import date, datetime, timedelta


from desktop_calendar.assistant.calendar_chat_protocol import ChatError, fail


def text(limit):
    return {"type": "string", "maxLength": limit}


FIELDS = {
    "title": {**text(300), "minLength": 1}, "date": text(10),
    "time": {"type": ["string", "null"]},
    "event_type": {"type": "string", "enum": ["general", "urgent", "ddl"]},
    "category_id": {"type": ["string", "null"]},
    "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "color_mode": {"type": "string", "enum": ["inherit", "override"]},
    "end_date": text(10),
    "category_basis": {"type": "string", "enum": ["explicit", "semantic_high"]},
    "reminder": {"type": ["integer", "null"], "minimum": 0, "maximum": 10080},
    "notes": text(2000), "duration_days": {"type": "integer", "minimum": 1, "maximum": 365},
    "skip_non_working_days": {"type": "boolean"}, "end_as_ddl": {"type": "boolean"},
}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


HABIT_FIELDS = {
    'title': {**text(300), 'minLength': 1},
    'frequency': {'type': 'string', 'enum': ['daily', 'weekly', 'monthly', 'yearly']},
    'interval': {'type': 'integer', 'minimum': 1, 'maximum': 999},
    'weekdays': {'type': 'array', 'minItems': 1, 'maxItems': 7,
                 'items': {'type': 'integer', 'minimum': 0, 'maximum': 6}},
    'month_day': {'type': 'integer', 'minimum': 1, 'maximum': 31},
    'year_month': {'type': 'integer', 'minimum': 1, 'maximum': 12},
    'start_on': text(10), 'end_on': {'type': ['string', 'null'], 'maxLength': 10},
    'workdays_only': {'type': 'boolean'},
    'color': FIELDS['color'], 'reminder_enabled': {'type': 'boolean'},
    'reminder_time': {'type': ['string', 'null'], 'maxLength': 5},
}


SCHEMAS = {
    'preview_create_habit': obj(HABIT_FIELDS, ('title', 'frequency')),
    'query_habits': obj({'keyword': text(100)}),
    'get_habit': obj({'habit_id': text(100)}, ('habit_id',)),
    'preview_update_habit': obj({'habit_id': text(100), 'changes': obj(HABIT_FIELDS)}, ('habit_id', 'changes')),
    "list_categories": obj({}),
    "query_events": obj({"start": text(10), "end": text(10), "keyword": text(100),
                         "category_id": {"type": ["string", "null"]}, "done": {"type": "boolean"},
                         "ddl_only": {"type": "boolean"}}),
    "get_event": obj({"event_id": text(100)}, ("event_id",)),
    "preview_create": obj(FIELDS, ("title", "date")),
    "preview_update": obj({"event_id": text(100), "changes": obj(FIELDS)}, ("event_id", "changes")),
    "preview_complete": obj({"event_id": text(100), "done": {"type": "boolean"}}, ("event_id", "done")),
    "preview_delete": obj({"event_id": text(100)}, ("event_id",)),
    "clarify": obj({"question": text(600)}, ("question",)),
}
SCHEMAS.update({
    'delete_events_batch': obj({'event_ids':{'type':'array','minItems':1,'maxItems':10,'items':text(100)}},('event_ids',)),
    'batch_create_events': obj({'items': {'type':'array', 'minItems':1, 'maxItems':10,
        'items':obj(FIELDS, ('title','date'))}, 'distinct_colors':{'type':'boolean'}}, ('items',)),
    'batch_update_events': obj({'items': {'type':'array', 'minItems':1, 'maxItems':10,
        'items':obj({'event_id':text(100), 'changes':obj(FIELDS)}, ('event_id','changes'))},
        'distinct_colors':{'type':'boolean'}}, ('items',)),
    'batch_set_completion': obj({'event_ids':{'type':'array','minItems':1,'maxItems':10,
        'items':text(100)}, 'done':{'type':'boolean'}}, ('event_ids','done')),
})
DESCRIPTIONS = {
    'delete_events_batch':'只准备当前完整删除候选集合的两级确认清单，不执行删除。需明确批量删除意图及真实稳定ID，不可单选代替全部。超过10条或截断须缩小范围。',
    'batch_create_events':'一次任务创建1至10个独立Event。items每项title/date与明确字段；随机不同颜色传distinct_colors=true，由本地固定选择。不是一个持续多日事项。',
    'batch_update_events':'一次原子修改1至10个已查得或recent_task中的Event ID，仅局部changes。不同颜色用distinct_colors=true；不得批量删除或修改Habit。',
    'batch_set_completion':'将已查得或recent_task中的1至10个Event设置done，整个任务一次提交，不是toggle。',
    'query_habits': '读取周期习惯候选ID/名称/周期。多个同名必须澄清，不得自动选第一条。',
    'get_habit': '读取本会话查得或创建的稳定Habit ID详情，与Event ID完全分离。',
    'preview_update_habit': '准备一个习惯的局部changes Patch，仅用户要求的字段；周期和属性修改保留历史打卡。宿主单对象安全保存。不得重新提交完整Habit。',
    'preview_create_habit': '创建一个真正周期习惯的提案，由宿主安全保存；不是Event或一次性待办。周期daily/weekly/monthly/yearly，interval为1–999；周一=0至周日=6，多选weekdays。起始默认本地今天，工作日默认false；月末不存在的日跳过。只填明确属性，未指定颜色/提醒/起止不要补全。',
    "list_categories": "读取现有分类的稳定ID和名称，供筛选及创建事项使用；不修改分类。",
    "query_events": "查询真实事项；日期为含端点 YYYY-MM-DD，范围最多366天。无条件查询仅本月。多结果不可自行选择。",
    "get_event": "读取本会话查询过的稳定Event ID的详情；不允许猜ID。",
    "preview_create": "仅预览一个新建事项，不执行。只提供用户明确给出的字段：默认只需title/date。不要补全持续天数、颜色、分类、提醒等默认值；无时间省略time，无分类省略category_id/color_mode。日期时间歧义先clarify。",
    "preview_update": "仅预览一个已明确定位事项的局部changes Patch；只传用户本次要求修改的字段！改名只传title，绝不重发完整Event或默认值。可合并同事项尚未确认变更；不执行。",
    "preview_complete": "仅预览完成或取消完成已明确定位的事项；不执行。",
    "preview_delete": "仅准备删除一个已明确定位事项的确认预览，必须由用户另行确认，不得自动删除。",
    "clarify": "信息不明确时向用户澄清，尤其日期/时间或相对提醒；不能宣称写入成功。",
}
TASK_TOOLS = {'batch_create_events','batch_update_events','batch_set_completion'}
READ_TOOLS = {'list_categories', 'query_events', 'get_event', 'query_habits', 'get_habit'}
PROPOSAL_TOOLS = {'preview_create', 'preview_update', 'preview_complete', 'preview_delete', 'preview_create_habit', 'preview_update_habit'}
PROPOSAL_TOOLS |= TASK_TOOLS
PROPOSAL_TOOLS.update(('delete_events_batch', 'propose_events','revise_event_target'))

EVENT_ADAPTERS = {'preview_create', 'preview_update', 'preview_complete',
                  'batch_create_events', 'batch_update_events', 'batch_set_completion',
                  'preview_delete', 'delete_events_batch'}
PLAN_ITEM = obj({'event_id': text(100), 'fields': obj(FIELDS), 'done': {'type':'boolean'}})
SCHEMAS['propose_events'] = obj({
    'action': {'type':'string', 'enum':['create','update','complete','delete']},
    'target': obj({'start':text(10),'end':text(10),
                   'titles':{'type':'array','maxItems':10,'items':{**text(300),'minLength':1}},
                   'quantity':{'type':'string','enum':['one','all_matches']}},
                  ('titles','quantity')),
    'fields':obj(FIELDS), 'done':{'type':'boolean'},
    'color_policy':{'type':'string','enum':['different_from_current']},
    'preview_only':{'type':'boolean'},
    'items': {'type':'array', 'minItems':1, 'maxItems':10, 'items':PLAN_ITEM},
    'distinct_colors': {'type':'boolean'}
}, ('action',))
SCHEMAS['revise_event_target'] = obj({'revision':text(100), **SCHEMAS['propose_events']['properties']['target']['properties']}, ('revision',))
SCHEMAS['clarify']['properties']['create_plan'] = obj({
    'action': {'type':'string','enum':['create']},
    'items': SCHEMAS['propose_events']['properties']['items'],
    'distinct_colors': {'type':'boolean'},
    'preview_only': {'type':'boolean'},
}, ('action','items'))
DESCRIPTIONS['clarify'] += ' 如果已有一份完整的创建方案，只等用户认可，请附上 create_plan，包含全部成员及字段；真正缺少字段时仅提问。'
DESCRIPTIONS['revise_event_target'] = '引用 awaiting_target.revision，提交用户补充的日期、标题或数量范围；宿主保留原动作和所有未修改字段。'
DESCRIPTIONS['propose_events'] = ('提交一个完整的同类事项计划。create 每个成员含 fields(title,date,...)；'
    'update 每个成员含 event_id 和 fields(只含修改值)；complete 含 event_id 和 done。'
    '创建使用items。修改、完成、删除优先用target替代items，无需先查ID。target用start/end含端点、titles精确标题数组'
    '（全部事项为[]）、quantity=one或all_matches；日期不明确时省略start/end。修改值用顶层fields或color_policy，'
    '完成值用顶层done。用户要求先预览时preview_only=true。宿主查询、选择、确认和执行。')
TOOLS = [{"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name], "parameters": schema}}
         for name, schema in SCHEMAS.items() if name not in EVENT_ADAPTERS]


def validate(value, schema, path='arguments'):
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    valid = {"null": value is None, "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool, "object": type(value) is dict, 'array': type(value) is list}
    if not any(valid.get(kind, False) for kind in types):
        fail("工具参数类型不正确。", field=path)
    if value is None:
        return
    if "enum" in schema and value not in schema['enum']:
        fail("工具参数不在允许范围内。", field=path)
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 2000):
            fail("文本长度不正确。", field=path)
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            fail("颜色格式不正确。", field=path)
    if type(value) is int and not schema.get('minimum', value) <= value <= schema.get('maximum', value):
        fail("数值超出允许范围。", field=path)
    if type(value) is dict:
        if set(value) - set(schema['properties']) or set(schema['required']) - set(value):
            fail("工具参数包含未知字段或缺少必需字段。", field=path)
        for key, item in value.items():
            validate(item, schema['properties'][key], path+'.'+key)
    if type(value) is list:
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 7):
            fail('数组长度不正确。', field=path)
        for item in value:
            validate(item, schema['items'], path+'[]')


def validate_call(name, arguments):
    if not isinstance(name, str) or name not in SCHEMAS:
        fail("此工具未开放。")
    validate(arguments, SCHEMAS[name])
    return arguments


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        fail("日期必须是 YYYY-MM-DD。")
    try:
        result = date.fromisoformat(value)
        if not 1900 <= result.year <= 2199:
            fail("日期应在1900至2199年内。")
        return result
    except ValueError:
        fail("日期不存在。")


def request_context(now=None):
    now = now or datetime.now().astimezone()
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    return {"now": now.isoformat(), "today": today.isoformat(), "tomorrow": (today+timedelta(days=1)).isoformat(),
            "this_week_start": monday.isoformat(), "this_week_end": (monday+timedelta(days=6)).isoformat(),
            "next_week_start": (monday+timedelta(days=7)).isoformat()}
