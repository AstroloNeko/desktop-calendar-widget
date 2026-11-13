"""Closed tool contract. No dynamic method dispatch or permissive Event coercion."""
import re
from datetime import date, datetime, timedelta


class ChatError(ValueError):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code = code
        self.field = field


def fail(message, code="invalid_arguments", field=None):
    raise ChatError(code, message, field)


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


SCHEMAS = {
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
DESCRIPTIONS = {
    "list_categories": "读取现有分类的稳定ID和名称，供筛选及创建事项使用；不修改分类。",
    "query_events": "查询真实事项；日期为含端点 YYYY-MM-DD，范围最多366天。无条件查询仅本月。多结果不可自行选择。",
    "get_event": "读取本会话查询过的稳定Event ID的详情；不允许猜ID。",
    "preview_create": "仅预览一个新建事项，不执行。只提供用户明确给出的字段：默认只需title/date。不要补全持续天数、颜色、分类、提醒等默认值；无时间省略time，无分类省略category_id/color_mode。日期时间歧义先clarify。",
    "preview_update": "仅预览一个已明确定位事项的局部changes Patch；只传用户本次要求修改的字段！改名只传title，绝不重发完整Event或默认值。可合并同事项尚未确认变更；不执行。",
    "preview_complete": "仅预览完成或取消完成已明确定位的事项；不执行。",
    "preview_delete": "仅准备删除一个已明确定位事项的确认预览，必须由用户另行确认，不得自动删除。",
    "clarify": "信息不明确时向用户澄清，尤其日期/时间或相对提醒；不能宣称写入成功。",
}
TOOLS = [{"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name], "parameters": schema}}
         for name, schema in SCHEMAS.items()]


def validate(value, schema, path='arguments'):
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    valid = {"null": value is None, "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool, "object": type(value) is dict}
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
