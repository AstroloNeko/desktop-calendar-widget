"""SQLite schema 1, independent of JSON schema 9. No automatic migration/import."""
SCHEMA_VERSION = 1
APPLICATION_ID = 0x4443414C

# Explicit model field mapping. Adding a model field requires an audited schema change.
COLUMNS = {
    'categories': dict(id='TEXT NOT NULL', name='TEXT NOT NULL', color='TEXT NOT NULL',
                      sort_order='INTEGER NOT NULL', created_at='TEXT NOT NULL'),
    'events': dict(id='TEXT NOT NULL', title='TEXT NOT NULL', due='TEXT NOT NULL', color='TEXT NOT NULL',
        event_type="TEXT NOT NULL CHECK(event_type IN ('general','urgent','ddl'))",
        reminder='INTEGER', notes='TEXT NOT NULL', done='INTEGER NOT NULL CHECK(done IN (0,1))',
        created_at='TEXT NOT NULL', snooze_until='TEXT', has_time='INTEGER NOT NULL CHECK(has_time IN (0,1))',
        duration_days='INTEGER NOT NULL CHECK(duration_days BETWEEN 1 AND 365)',
        skip_non_working_days='INTEGER NOT NULL', end_as_ddl='INTEGER NOT NULL', category_id='TEXT',
        color_mode="TEXT NOT NULL CHECK(color_mode IN ('inherit','override'))"),
    'routines': dict(id='TEXT NOT NULL', title='TEXT NOT NULL', kind="TEXT NOT NULL CHECK(kind IN ('habit','todo'))",
        color='TEXT NOT NULL', created_on='TEXT NOT NULL', completed_on='TEXT', enabled='INTEGER NOT NULL',
        reminder_enabled='INTEGER NOT NULL', reminder_time='TEXT',
        frequency="TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly','yearly'))",
        interval='INTEGER NOT NULL CHECK(interval BETWEEN 1 AND 999)', month_day='INTEGER', year_month='INTEGER',
        start_on='TEXT', end_on='TEXT', workdays_only='INTEGER NOT NULL'),
}


def statements():
    yield 'CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)'
    yield 'CREATE TABLE calendars(id TEXT PRIMARY KEY, name TEXT NOT NULL, timezone TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0))'
    for table, columns in COLUMNS.items():
        fields = ','.join(f'"{name}" {kind}' for name, kind in columns.items())
        extra = ''
        if table == 'events':
            extra = ', FOREIGN KEY(calendar_id,category_id) REFERENCES categories(calendar_id,id) DEFERRABLE INITIALLY DEFERRED'
        yield (f'CREATE TABLE {table} (calendar_id TEXT NOT NULL REFERENCES calendars(id), {fields}, '
               'order_key INTEGER NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(calendar_id,id), '
               "CHECK(length(trim(id))>0)" + extra + ')')
    for table, value in (
        ('routine_weekdays', 'weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6)'),
        ('habit_completions', 'occurrence_date TEXT NOT NULL'),
        ('habit_schedule_history', 'until_date TEXT NOT NULL, rule_json TEXT NOT NULL CHECK(json_valid(rule_json))'),
    ):
        key = value.split()[0]
        yield (f'CREATE TABLE {table}(calendar_id TEXT NOT NULL, routine_id TEXT NOT NULL, {value}, '
               f'order_key INTEGER NOT NULL, PRIMARY KEY(calendar_id,routine_id,{key}), '
               'FOREIGN KEY(calendar_id,routine_id) REFERENCES routines(calendar_id,id) ON DELETE CASCADE)')
    yield "CREATE TABLE date_states(calendar_id TEXT NOT NULL REFERENCES calendars(id), day TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('leave','holiday')), PRIMARY KEY(calendar_id,day))"
    yield "CREATE TABLE preferences(calendar_id TEXT NOT NULL REFERENCES calendars(id), scope TEXT NOT NULL CHECK(scope IN ('device','reminder','unclassified')), key TEXT NOT NULL, value_json TEXT NOT NULL CHECK(json_valid(value_json)), PRIMARY KEY(calendar_id,key))"
    yield 'CREATE TABLE reminder_delivery_state(calendar_id TEXT NOT NULL REFERENCES calendars(id), plan_key TEXT NOT NULL, PRIMARY KEY(calendar_id,plan_key))'
    yield 'CREATE TABLE operation_receipts(operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, committed_revision INTEGER NOT NULL, changed INTEGER NOT NULL, operation_type TEXT NOT NULL, affected_ids TEXT NOT NULL, committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)'
    yield 'CREATE INDEX event_due_idx ON events(calendar_id,due,id)'
    yield 'CREATE INDEX event_category_idx ON events(calendar_id,category_id,done)'
    yield 'CREATE INDEX routine_enabled_idx ON routines(calendar_id,enabled)'


def preference_scope(key):
    if 'reminder' in key or key == 'notifications_enabled':
        return 'reminder'
    if key in {'theme','opacity','window_mode','agenda_open','show_holidays','global_display_mode',
               'global_geometry','compact_geometry','hotkey','crisp_text_migrated_v1'} or 'geometry' in key or 'hotkey' in key:
        return 'device'
    # Unknown settings are retained, never automatically labelled syncable core facts.
    return 'unclassified'
