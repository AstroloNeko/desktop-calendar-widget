"""Display-only fields from verified reminder and brief business responses."""
from desktop_calendar.reminders.reminder_config import FIELDS, impacts


def event_reminder(row):
    plan = row.get('reminder_plan')
    if not plan:
        return {}
    fields = ('mode','planned_at','snoozed_at','global_enabled','past','catchup_eligible','schedule_status')
    value = {key:plan[key] for key in fields if key in plan}
    rule = plan.get('rule') or {}
    mode = plan['mode']
    if mode == 'absolute':
        label = '固定提醒，日程改期时保留提醒时刻'
    elif mode == 'relative':
        lead = (f"{rule['days']} 天" if 'days' in rule else
                f"{rule.get('minutes', row.get('reminder'))} 分钟" if rule.get('minutes',row.get('reminder')) is not None else '')
        label = '提前 '+lead if lead else '相对提醒'
    else:
        label = '默认提醒' if mode == 'default' else '不提醒'
    if plan.get('past'):
        label += ' · 已到或超过提醒时间'
        if plan.get('catchup_eligible'):
            label += '，可补发'
    value['rule_summary'] = label
    return {'reminder_plan':value}


def channel_projection(row, channel):
    label = row[channel]
    status = row['send_states'].get(channel)
    if status in ('user_opt_out','rate_limited'):
        status = 'failed'
    if status not in ('disabled','waiting','pending','dispatching','submitted','sent','failed',
                      'unknown','expired','cancelled','skipped','exhausted','not_included'):
        status = {'未启用':'disabled','未列入':'not_included','本次未列入':'not_included',
                  '已过期':'expired','待处理':'pending'}.get(label,'waiting')
    value = {'status':status, 'label':label[:64]}
    if reason := row.get('channel_reasons', {}).get(channel):
        value['reason'] = reason[:200]
    return value


def reminder_rows(rows):
    return [
        {'index':index, 'title':row['title'][:200], 'when':row['when'], 'status':row['status'][:64],
         'group':row['group'], 'snoozed':row['snoozed'], 'missed':row['missed'],
         'processed':bool(row['processed_at']), 'event_done':row['done'], 'next_attempt':row['next_attempt'],
         'channels':{key:channel_projection(row,key) for key in ('windows','qq')}}
        for index,row in enumerate(rows[:30],1)
    ]


def reminder_fields(receipt, response):
    if query := response.get('reminder_query'):
        rows = reminder_rows(query['rows'])
        # A per-event rule description is outside the reminder-instance schema.
        receipt.update(result_type='message' if query.get('item_details') else 'reminder_query', reminders=rows,
                       reminder_summary={'enabled':query['enabled'],'total':query['total'],'shown':len(rows)})
    if brief := response.get('brief_result'):
        items = [{**{key:item[key] for key in ('group','title','date','time')},'index':index}
                 for index,item in enumerate(brief['items'][:30],1)]
        receipt.update(result_type='brief', brief={
            **{key:brief[key] for key in ('date','scope','private','counts','agenda_count')},
            'total':sum(brief['counts'].values()), 'items':items,
        })
    result = response.get('operation_result') or {}
    preview = (response.get('pending_operation') or {}).get('preview',{})
    if result.get('action') == 'reminder_action' and result.get('goal_satisfied') is True:
        receipt.update(result_type='reminder_action',
                       reminder_summary={'action_kind':result['kind'], **{
                           key:result[key] for key in ('count','until','scheduling_pending') if key in result}})
    plan = preview.get('configuration_plan', {})
    changes = result.get('fields') if result.get('action') == 'reminder_config' else (
        plan.get('fields') if receipt['result_type'] == 'confirmation' else None)
    if changes:
        scope = {'brief' if field.startswith('brief.') else 'reminder_global' for field in changes}
        def display(value):
            return '开启' if value is True else '关闭' if value is False else str(value)
        authorized = bool(result.get('remote_authorization') if result else plan.get('remote_authorization'))
        qq_status = result.get('qq_status') or plan.get('qq_status')
        receipt['config_diff'] = {'scope':next(iter(scope)) if len(scope) == 1 else 'mixed',
                                  'requires_remote_authorization':authorized,
                                  'impact_summary':impacts(changes),
                                  'changes':[{'field':field,'label':FIELDS[field][0],
                                              'before':display(edit['before']),'after':display(edit['after'])}
                                             for field,edit in changes.items()]}
        if qq_status:
            receipt['config_diff']['capability_notice'] = {
                'available':'QQ 主动推送能力已核验。','unavailable':'QQ 主动推送当前不可用。',
                'unsupported':'当前云端没有主动推送接口。','unbound':'当前没有可用的已核验 QQ 主动推送绑定。',
                'unverified':'当前 QQ 主动推送能力尚未核验。',
            }.get(qq_status,'')
        if result.get('goal_satisfied') is True:
            receipt['result_type'] = 'config_update'
            receipt['summary'] = ('提醒配置已保存，已核验隐私授权。' if result.get('remote_authorization')
                                  else '提醒配置已保存。')
        elif plan.get('remote_authorization'):
            receipt['summary'] = '等待明确回复「授权确认」。'
