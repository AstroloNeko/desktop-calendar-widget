"""Explicit v0.10 JSON8 compatibility. No Store, clock, I/O or tolerant loading."""
from copy import deepcopy
from datetime import date

CONVERTER_VERSION = 'v010-schema8-v1'
EVIDENCE = 'v0.10.2@cb0c62b204c553f61cb31b83955e1191f675c2e2'
EVENT_DEFAULTS = dict(color='#6687F2',event_type='general',reminder=60,notes='',done=False,
    snooze_until=None,has_time=True,duration_days=1,skip_non_working_days=False,
    end_as_ddl=False,category_id=None,color_mode='override')
ROUTINE_DEFAULTS = dict(kind='habit',color='#52B788',completed_on=None,habit_done=[],enabled=True,
    reminder_enabled=False,reminder_time=None,frequency='daily',interval=1,end_on=None)
CATEGORY_DEFAULTS = dict(color='#6687F2',sort_order=0)
ALIASES = {'low':'general','normal':'general','general':'general','urgent':'urgent','ddl':'ddl',
    '低':'general','低优先级':'general','普通':'general','一般':'general','普通优先级':'general',
    '一般优先级':'general','高':'urgent','高优先级':'urgent','紧急':'urgent','紧急优先级':'urgent',
    'DDL':'ddl','deadline':'ddl','截止':'ddl'}


def convert_schema8(raw, result):
    """Only proven omissions/aliases are accepted; malformed values remain errors.

    Strict canonical validation follows this version-specific conversion. Unknown
    fields are not filtered. Source objects are never modified.
    """
    out=deepcopy(raw)
    rules=[]; defaults=[]
    result.compatibility=dict(source_app_version=None,source_schema_version=8,
        target_sqlite_schema_version=1,converter_version=CONVERTER_VERSION,
        applied_conversion_rules=rules,legacy_defaults_applied=defaults,
        historical_information_limitations=['Schema 8 未保存周期变更历史；空历史保留旧版当前规则解释全部日期的行为，不能恢复不存在的历史。'],
        field_mapping_verified=False,unsupported_legacy_fields=[])
    def record(path, rule, evidence, default=False):
        item=dict(source_path=path,target_path=path,rule=rule,lossless_semantics=True,
                  evidence=EVIDENCE+':'+evidence)
        rules.append(item)
        if default:defaults.append(item.copy())
    def fill(row,key,value,path,evidence):
        if key not in row:
            row[key]=deepcopy(value)
            record(path+'.'+key,'legacy_default',evidence,True)
    for key,value in dict(events=[],routines=[],categories=[],date_states={},settings={},notified=[]).items():
        fill(out,key,value,'$','calendar_core.py:Store.load')
    for table,values in (('events',EVENT_DEFAULTS),('categories',CATEGORY_DEFAULTS),('routines',ROUTINE_DEFAULTS)):
        rows=out.get(table)
        if not isinstance(rows,list):continue
        for i,row in enumerate(rows):
            if not isinstance(row,dict):continue
            path=f'$.{table}[{i}]'
            if table=='events':
                supplied=[row[k] for k in ('event_type','type','priority') if k in row and row[k] is not None]
                if any(not isinstance(v,str) or v.strip() not in ALIASES for v in supplied):
                    result.issue('invalid_legacy_event_type',path,'旧事项性质无效，不使用宽容回退。')
                elif len({ALIASES[v.strip()] for v in supplied})>1:
                    result.issue('conflicting_legacy_alias',path,'多个性质字段含义冲突，不能丢弃其中一个。')
                else:
                    kind=ALIASES[supplied[0].strip()] if supplied else 'general'
                    if 'is_ddl' in row and type(row['is_ddl']) is not bool:
                        result.issue('invalid_field_type',path+'.is_ddl','必须是布尔值。')
                    if row.get('is_ddl') is True:kind='ddl'
                    if any(k in row for k in ('type','priority','is_ddl')) or row.get('event_type')!=kind:
                        record(path+'.event_type','legacy_event_type_precedence','calendar_core.py:Event.from_dict / LEGACY_EVENT_TYPE_MAP')
                    for alias in ('type','priority','is_ddl'):
                        if alias in row:
                            record(path+'.'+alias,'legacy_alias_to_event_type','calendar_core.py:Event.from_dict')
                            rules[-1]['target_path']=path+'.event_type'
                    row['event_type']=kind
                for key in ('type','priority','is_ddl'):row.pop(key,None)
                # Untimed events never had minute reminders in the old constructor.
                if row.get('has_time') is False and 'reminder' not in row:
                    fill(row,'reminder',None,path,'calendar_core.py:Event.__post_init__')
                if row.get('color_mode')=='inherit' and 'color' not in row and isinstance(out.get('categories'),list):
                    category=next((c for c in out['categories'] if isinstance(c,dict) and c.get('id')==row.get('category_id')),None)
                    if category is not None:
                        fill(row,'color',category.get('color',CATEGORY_DEFAULTS['color']),path,'calendar_core.py:Store.load category inheritance')
            if table=='routines':
                if 'schedule_history' in row:
                    result.issue('unsupported_legacy_field',path+'.schedule_history','正式 Schema 8 不存在周期历史字段。')
                    result.compatibility['unsupported_legacy_fields'].append(path+'.schedule_history')
                fill(row,'workdays_only','frequency' not in row,path,'calendar_core.py:RoutineItem.from_dict')
            for key,value in values.items():fill(row,key,value,path,'calendar_core.py:'+{'events':'Event','routines':'RoutineItem','categories':'EventCategory'}[table])
            if table=='routines':
                # Dates derived only from an explicit persisted creation/anchor date.
                try:anchor=date.fromisoformat(row.get('start_on') or row['created_on'])
                except (ValueError,TypeError,KeyError):
                    result.issue('unprovable_legacy_default',path+'.start_on','缺少可靠周期锚点，不能使用今天。');continue
                for key,value in dict(start_on=anchor.isoformat(),weekdays=[anchor.weekday()],month_day=anchor.day,year_month=anchor.month).items():
                    if key not in row:fill(row,key,value,path,'habit_schedule.py:normalize_schedule')
                    elif (row[key] is None and key!='weekdays') or (key=='weekdays' and row.get(key)==[]):
                        row[key]=value;record(path+'.'+key,'anchor_default','habit_schedule.py:normalize_schedule',True)
                row['schedule_history']=[]
                record(path+'.schedule_history','empty_history_preserves_legacy_rule','f7e622c:RoutineItem.schedule_history / habit_schedule.rule_on',True)
    out['version']=9
    record('$.version','validated_schema8_to_canonical_snapshot','data_compat.py:DATA_SCHEMA_VERSION')
    return out
