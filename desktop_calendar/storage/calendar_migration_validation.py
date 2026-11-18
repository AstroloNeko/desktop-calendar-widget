"""Strict Schema 9 validation, deliberately separate from tolerant Store.load.

No file writes, no user-path discovery, no clocks used to invent missing fields.
Reports contain paths/codes/IDs but never field values, titles, notes or settings.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time

from calendar_core import Event, RoutineItem, EventCategory
from habit_schedule import SCHEDULE_FIELDS, validate_schedule_input
from desktop_calendar.storage.sqlite_calendar_schema import preference_scope

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MODELS = {'events':Event, 'routines':RoutineItem, 'categories':EventCategory}
ROOT_FIELDS = {'version','events','routines','categories','date_states','settings','notified'}
BOOLEAN_FIELDS = {'done','has_time','skip_non_working_days','end_as_ddl','enabled','reminder_enabled','workdays_only'}
STRING_FIELDS = {'id','title','name','color','created_at','created_on','due','notes','event_type','color_mode','kind','frequency'}
NULLABLE_STRINGS = {'category_id','snooze_until','completed_on','reminder_time','start_on','end_on'}
INTEGER_FIELDS = {'sort_order','duration_days','interval','month_day','year_month'}


@dataclass(frozen=True)
class MigrationIssue:
    code: str
    source_path: str
    message: str
    object_type: str = 'document'
    object_id: str | None = None
    severity: str = 'error'
    blocks_migration: bool = True


@dataclass
class ValidationResult:
    source: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    normalizations: list = field(default_factory=list)
    preference_scopes: dict = field(default_factory=dict)
    elapsed_ms: float = 0
    compatibility: dict = field(default_factory=dict)
    _document: dict | None = field(default=None, repr=False)

    @property
    def valid(self):
        return self._document is not None and not any(i.blocks_migration for i in self.issues)

    def report(self):
        return dict(source=self.source,counts=self.counts,valid=self.valid,
            issues=[asdict(i) for i in self.issues],normalizations=self.normalizations,
            preference_scopes=self.preference_scopes,elapsed_ms=self.elapsed_ms,compatibility=self.compatibility)

    def issue(self,code,path,message,kind='document',identifier=None):
        self.issues.append(MigrationIssue(code,path,message,kind,identifier))


class _Pairs(list): pass


def _path(parent, key):
    return parent + '[' + json.dumps(key,ensure_ascii=True) + ']'


def _decode(data, result):
    def constant(_value): raise ValueError('Non-finite number')
    raw = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_Pairs,parse_constant=constant)
    def visit(value,path):
        if isinstance(value,_Pairs):
            out={}
            for key,item in value:
                if key in out: result.issue('duplicate_json_key',_path(path,key),'JSON 对象中存在重复键，不能选择其中一个值。')
                out[key]=visit(item,_path(path,key))
            return out
        if isinstance(value,list): return [visit(item,f'{path}[{i}]') for i,item in enumerate(value)]
        if isinstance(value,str): value.encode('utf8')  # Reject unpaired surrogates.
        if isinstance(value,float):
            import math
            if not math.isfinite(value): raise ValueError('Non-finite number')
        return value
    return visit(raw,'$')


def _day(value):
    return isinstance(value,str) and date.fromisoformat(value).isoformat()==value


def _timestamp(value):
    if not isinstance(value,str) or not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}',value):
        return False
    return datetime.fromisoformat(value).tzinfo is None


def _schedule(rule):
    if not isinstance(rule,dict) or set(rule)!=set(SCHEDULE_FIELDS): return False
    if type(rule['interval']) is not int or type(rule['month_day']) is not int or type(rule['year_month']) is not int:
        return False
    if type(rule['workdays_only']) is not bool or not isinstance(rule['weekdays'],list): return False
    if any(type(d) is not int for d in rule['weekdays']): return False
    if not _day(rule['start_on']) or (rule['end_on'] is not None and not _day(rule['end_on'])): return False
    return validate_schedule_input(**rule)==rule


def _record(row,table,index,result):
    path=f'$.{table}[{index}]'
    if not isinstance(row,dict):
        result.issue('invalid_record',path,'业务记录必须是对象。',table)
        return
    identifier=row.get('id') if isinstance(row.get('id'),str) else None
    def fail(code,key,message): result.issue(code,path+('.'+key if key else ''),message,table,identifier)
    model=MODELS[table]
    names={f.name for f in fields(model)}
    for key in sorted(names-row.keys()): fail('missing_required_field',key,'严格 Schema 9 导入需要完整字段；不能猜测历史默认值。')
    for key in sorted(row.keys()-names): fail('unmapped_business_field',key,'业务字段没有 SQLite1 映射，不能丢弃。')
    if names != row.keys(): return
    begin=len(result.issues)
    for key,value in row.items():
        if key in BOOLEAN_FIELDS and type(value) is not bool: fail('invalid_field_type',key,'必须是布尔值。')
        if key in STRING_FIELDS and not isinstance(value,str): fail('invalid_field_type',key,'必须是字符串。')
        if key in NULLABLE_STRINGS and value is not None and not isinstance(value,str): fail('invalid_field_type',key,'必须为字符串或 null。')
        if key in INTEGER_FIELDS and (type(value) is not int or not -(2**63)<=value<2**63): fail('invalid_field_type',key,'必须为可保存的整数。')
    if len(result.issues)!=begin: return
    if not row['id'].strip(): fail('empty_id','id','ID 不能为空；不能重新编号。')
    if not row.get('title',row.get('name','')).strip(): fail('empty_title','title' if table!='categories' else 'name','名称不能为空。')
    if not re.fullmatch(r'#[0-9A-Fa-f]{6}',row['color']): fail('invalid_color','color','必须是合法六位颜色值。')
    for key in ('due','created_at','snooze_until'):
        if key in row and row[key] is not None:
            try: valid=_timestamp(row[key])
            except ValueError: valid=False
            if not valid: fail('invalid_due' if key=='due' else 'unsupported_datetime',key,'需要可验证的本地无时区 ISO 日期时间；不推测时区或创建时间。')
    if table=='events':
        if row['event_type'] not in ('general','urgent','ddl'): fail('invalid_event_type','event_type','事项性质无效。')
        if not 1<=row['duration_days']<=365: fail('invalid_duration','duration_days','持续天数须在1至365之间。')
        if row['color_mode'] not in ('override','inherit'): fail('invalid_color_mode','color_mode','颜色来源无效。')
        if row['category_id'] is None and row['color_mode']=='inherit': fail('invalid_category_reference','category_id','无分类事项不能继承分类颜色。')
        if row['reminder'] is not None and (type(row['reminder']) is not int or not 0<=row['reminder']<2**63): fail('invalid_reminder','reminder','提醒须为非负整数或 null。')
        if not row['has_time'] and row['reminder'] is not None: fail('inconsistent_untimed_reminder','reminder','无具体时间与分钟提醒不一致，不能静默清除。')
        if row['end_as_ddl'] and (row['duration_days']==1 or row['event_type']=='ddl'): fail('invalid_end_as_ddl','end_as_ddl','末日DDL字段与原生DDL或单日事项冲突。')
    if table=='routines':
        if row['kind'] not in ('habit','todo'): fail('invalid_routine_kind','kind','习惯/待办类型无效。')
        for key in ('created_on','completed_on'):
            try: valid=row[key] is None and key=='completed_on' or _day(row[key])
            except (ValueError,TypeError): valid=False
            if not valid: fail('invalid_date',key,'需要标准 YYYY-MM-DD 日期。')
        try: valid=_schedule({k:row[k] for k in SCHEDULE_FIELDS})
        except (ValueError,TypeError): valid=False
        if not valid: fail('invalid_habit_rule','frequency','重复规则无效或需要有损/未定义的规范化。')
        clock=row['reminder_time']
        if clock is not None and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',clock): fail('invalid_reminder','reminder_time','提醒时间须为 HH:MM。')
        if row['reminder_enabled'] and clock is None: fail('invalid_reminder','reminder_time','开启提醒时必须具有明确时间。')
        done=row['habit_done']
        try: valid=isinstance(done,list) and all(_day(d) for d in done) and len(set(done))==len(done)
        except (ValueError,TypeError): valid=False
        if not valid: fail('invalid_habit_completion','habit_done','打卡历史必须为唯一合法日期列表，不删除或生成记录。')
        history=row['schedule_history']
        if not isinstance(history,list): fail('invalid_habit_history','schedule_history','规则历史必须为列表。')
        else:
            boundaries=[]
            for n,entry in enumerate(history):
                try:
                    valid=isinstance(entry,dict) and set(entry)=={'until','rule'} and _day(entry['until']) and _schedule(entry['rule'])
                except (ValueError,TypeError): valid=False
                if not valid: fail('invalid_habit_history',f'schedule_history[{n}]','历史规则/排他边界无效。')
                else: boundaries.append(entry['until'])
            if boundaries!=sorted(set(boundaries)): fail('invalid_habit_history','schedule_history','历史边界重复或乱序，不能重建历史。')
    if len(result.issues)!=begin: return
    try:
        # Model construction is only a cross-check AFTER field/type validation.
        restored=asdict(model.from_dict(deepcopy(row)))
        for key in names:
            if restored[key]!=row[key]: fail('noncanonical_record',key,'当前模型会改变该值；严格导入拒绝自动修复或重排。')
    except (ValueError,TypeError,KeyError,OverflowError):
        fail('invalid_record','','记录无法无损映射到当前模型。')


def _validate_document(raw,result):
    if not isinstance(raw,dict):
        result.issue('malformed_json','$','根节点必须是对象。'); return
    result.source['json_schema']=raw.get('version') if type(raw.get('version')) is int else None
    if type(raw.get('version')) is not int or raw['version']!=9:
        result.issue('unsupported_schema','$.version','B3a 只导入完整 Schema 9；旧版需显式转换，未来版不降级。'); return
    for key in sorted(ROOT_FIELDS-raw.keys()): result.issue('missing_required_field','$.'+key,'Schema 9 顶层字段缺失。')
    for key in sorted(raw.keys()-ROOT_FIELDS): result.issue('unmapped_business_field',_path('$',key),'未映射的顶层字段不能丢弃。')
    counts={}
    for table in MODELS:
        rows=raw.get(table)
        counts[table]=len(rows) if isinstance(rows,list) else None
        if not isinstance(rows,list): result.issue('invalid_collection','$.'+table,'集合必须是列表。'); continue
        seen=set()
        for index,row in enumerate(rows):
            _record(row,table,index,result)
            if isinstance(row,dict) and isinstance(row.get('id'),str):
                identifier=row['id']
                if identifier in seen: result.issue('duplicate_'+{'events':'event','routines':'habit','categories':'category'}[table]+'_id',f'$.{table}[{index}].id','ID 重复；同名同日但不同 ID 不属于此错误。',table,identifier)
                seen.add(identifier)
    routines=raw.get('routines',[])
    for key,field_name in (('habit_completions','habit_done'),('habit_schedule_history','schedule_history')):
        counts[key]=sum(len(r[field_name]) for r in routines if isinstance(r,dict) and isinstance(r.get(field_name),list)) if isinstance(routines,list) else None
    categories={c['id']:c for c in raw.get('categories',[]) if isinstance(c,dict) and isinstance(c.get('id'),str)} if isinstance(raw.get('categories'),list) else {}
    if isinstance(raw.get('events'),list):
        for i,e in enumerate(raw['events']):
            if not isinstance(e,dict): continue
            cid=e.get('category_id')
            if cid is not None:
                if not isinstance(cid,str) or cid not in categories:
                    result.issue('invalid_category_reference',f'$.events[{i}].category_id','引用的分类不存在。','events',e.get('id') if isinstance(e.get('id'),str) else None)
                elif e.get('color_mode')=='inherit' and e.get('color')!=categories[cid].get('color'):
                    result.issue('inherited_color_mismatch',f'$.events[{i}].color','原始颜色与分类继承回退颜色不一致；拒绝覆盖原始颜色。')
    states=raw.get('date_states')
    counts['date_states']=len(states) if isinstance(states,dict) else None
    if not isinstance(states,dict): result.issue('invalid_date_states','$.date_states','日期状态必须是对象。')
    else:
        for day,status in list(states.items()):
            try: valid=_day(day)
            except ValueError: valid=False
            if not valid or status not in ('normal','leave','holiday'):
                result.issue('invalid_date_state',_path('$.date_states',day),'日期或覆盖值无效；不改写法定日历。')
            elif status=='normal':
                del states[day]
                result.normalizations.append(dict(source_path=_path('$.date_states',day),code='normal_means_no_override',lossless_semantics=True,message='normal 等价于没有用户覆盖，恢复系统日历判断。'))
    settings=raw.get('settings')
    counts['preference_keys']=len(settings) if isinstance(settings,dict) else None
    if not isinstance(settings,dict): result.issue('invalid_preferences','$.settings','偏好必须为对象。')
    else:
        for key in settings:
            scope=preference_scope(key)
            result.preference_scopes[scope]=result.preference_scopes.get(scope,0)+1
    notified=raw.get('notified')
    counts['reminder_delivery']=len(notified) if isinstance(notified,list) else None
    if not isinstance(notified,list) or any(not isinstance(v,str) for v in notified):
        result.issue('invalid_reminder_ledger','$.notified','投递记录必须为字符串列表。')
    elif len(set(notified))!=len(notified): result.issue('duplicate_reminder_ledger','$.notified','重复投递键不能静默合并。')
    else:
        if notified != sorted(notified):
            result.normalizations.append(dict(source_path='$.notified',code='ledger_set_order',lossless_semantics=True,message='投递记录为集合语义，不保留列表排列；保留全部唯一键。'))
        raw['notified']=sorted(notified)
    result.counts=counts
    result._document=raw


def validate_source(source):
    """Read explicit bytes once; return private plan + public redacted validation report."""
    started=time.perf_counter()
    result=ValidationResult(source={'path':str(Path(source).resolve()),'sha256':None,'json_schema':None})
    try:
        with Path(source).open('rb') as stream:
            stat=os.fstat(stream.fileno())
            data=stream.read(MAX_SOURCE_BYTES+1)
        result.source.update(size_bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,
                             sha256=hashlib.sha256(data).hexdigest() if len(data)<=MAX_SOURCE_BYTES else None)
        if len(data)>MAX_SOURCE_BYTES:
            result.issue('source_too_large','$','源文件超过严格预演的32MiB上限；未解析。')
        else:
            raw=_decode(data,result)
            source_schema=raw.get('version') if isinstance(raw,dict) else None
            if type(source_schema) is int and source_schema==8:
                from desktop_calendar.storage.calendar_legacy_migration import convert_schema8
                raw=convert_schema8(raw,result)
            _validate_document(raw,result)
            result.source['json_schema']=source_schema if type(source_schema) is int else None
            if result.compatibility:
                result.compatibility['field_mapping_verified']=result.valid
    except OSError:
        result.issue('source_unreadable','$','无法只读访问指定源文件。')
    except (UnicodeError,ValueError,RecursionError,TypeError,OverflowError):
        result.issue('malformed_json','$','JSON 编码、结构、数值或嵌套深度无效。')
    result.elapsed_ms=round((time.perf_counter()-started)*1000,3)
    return result
