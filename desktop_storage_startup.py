"""Desktop authority selection, before Tk services can read or write a Store.

Migration is opt-in and restricted to an explicitly supplied isolated directory.
The session owns the repository and directory lease for the desktop lifetime.
"""
from dataclasses import dataclass
from pathlib import Path

from calendar_core import DATA_DIR, Store, DataCompatibilityError
from calendar_storage_lifecycle import StorageLifecycle, LifecycleError, digest

# Retain ownership if native shutdown fails; never unlock beneath live producers.
_QUARANTINED_DESKTOPS = []


@dataclass(frozen=True)
class StartupOptions:
    isolated_directory: Path | None = None
    offer_migration: bool = False

    def manager(self):
        if self.isolated_directory is None:
            if self.offer_migration:
                raise LifecycleError('explicit_isolated_directory_required')
            return StorageLifecycle(DATA_DIR, existing_only=True)
        return StorageLifecycle(self.isolated_directory, isolated=True)


class DesktopStorageSession:
    def __init__(self, manager, lease, state, store):
        self.manager, self.lease, self.state, self.store = manager, lease, state, store
        self.closed = False

    @property
    def sqlite(self):
        return self.state['state'] == 'ACTIVE'

    def close(self):
        # Caller must stop producers and the Tk dispatcher before closing.
        # If close fails, retain the lease rather than allow a competing writer.
        if not self.closed:
            self.store.repository.close()
            self.lease.__exit__(None, None, None)
            self.closed = True


def open_desktop_storage(options=StartupOptions(), *, confirm=lambda: False,
                         notify=lambda state: None):
    manager = options.manager()
    lease = manager.lease()
    lease.__enter__()
    store = None
    try:
        state = manager._read()
        if state['state'] == 'NOT_STARTED' and options.offer_migration:
            source = manager._path('calendar_data.json')
            original = digest(source)
            if confirm():
                # migrate() owns its own exclusive lease and revalidates source.
                # A contender in this narrow gap makes startup fail, not bypass locking.
                lease.__exit__(None, None, None)
                if digest(source) != original:
                    raise LifecycleError('source_changed')
                notify('migrating')
                manager.migrate(expected_source_hash=original)
                lease.__enter__()
                notify('activated')
        state, path, factory = manager._select()
        # Restore replay/epoch UI belongs to C2. Never start desktop reminders or
        # reuse old chat pending operations until that review is implemented.
        if state.get('operation') == 'restore':
            raise LifecycleError('restored_desktop_requires_review_c2')
        store = Store(path, repository_factory=factory)
        if store.load_error:
            raise DataCompatibilityError(store.load_error)
        return DesktopStorageSession(manager, lease, state, store)
    except BaseException:
        if store is not None:
            store.repository.close()
        lease.__exit__(None, None, None)
        raise


def parse_startup_options(arguments):
    import argparse
    parser = argparse.ArgumentParser(description='Desktop Calendar')
    parser.add_argument('--isolated-data-dir', type=Path)
    parser.add_argument('--offer-storage-migration', action='store_true')
    args = parser.parse_args(arguments)
    if args.offer_storage_migration and args.isolated_data_dir is None:
        parser.error('--offer-storage-migration requires --isolated-data-dir')
    return StartupOptions(args.isolated_data_dir, args.offer_storage_migration)


def stop_desktop_storage(app):
    session = getattr(app, 'storage_session', None)
    if session is None or session.closed:
        return
    try:
        state = getattr(app, 'lifecycle', None)
        if state is not None:
            state.exiting = state.stopped = True
        chat = getattr(app, 'chat_runtime', None)
        if chat is not None:
            chat.stop()
        app.stop_native_reminders()
        tray = getattr(app, 'tray_icon', None)
        if tray is not None:
            tray.stop()
            app.tray_icon = None
        # All business writes are owner-thread dispatched. Exiting cancels late
        # queued writes; stopping HTTP/model admission does not wait on Tk itself.
        session.close()
    except BaseException:
        _QUARANTINED_DESKTOPS.append(app)
        raise


def create_desktop_app(options, app_class, *, instance=None, **callbacks):
    session = open_desktop_storage(options, **callbacks)
    app = app_class.__new__(app_class)
    app.tk = None  # Tk.__getattr__ must be safe even if Tcl initialization fails.
    app.storage_session = session
    try:
        app_class.__init__(app, store=session.store, instance=instance)
        return app
    except BaseException:
        stop_desktop_storage(app)
        # Constructor failures must not leave a hidden Tk root and timers alive.
        if getattr(app, 'tk', None) is not None:
            try:
                for job in app.tk.call('after', 'info'):
                    app.after_cancel(job)
                app.destroy()
            except Exception:
                pass  # Preserve the startup exception; authority is already closed.
        raise
