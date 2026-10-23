// In-box WinRT bridge; Windows SDK/MSVC only. No Windows App SDK runtime.
// Python receives immutable event strings, never a Tk/window pointer.
#include <windows.h>
#include <shobjidl.h>
#include <propkey.h>
#include <propvarutil.h>
#include <notificationactivationcallback.h>
#include <winrt/Windows.Data.Xml.Dom.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.UI.Notifications.h>
#include <map>
#include <algorithm>
#include <deque>
#include <memory>
#include <mutex>
#include <string>

using namespace winrt;
using namespace Windows::Data::Xml::Dom;
using namespace Windows::UI::Notifications;
using Callback = void(__stdcall*)(int, wchar_t const*);
static thread_local std::wstring last_error;
struct Apartment {
    Apartment() { init_apartment(apartment_type::multi_threaded); }
    ~Apartment() { uninit_apartment(); }
};
struct Delivery { ToastNotification toast{nullptr}; event_token activated{}, failed{}, dismissed{}; };
struct Shared {
    std::mutex mutex;
    Callback callback{};
    bool closing{};
    void emit(int kind, std::wstring const& value) {
        std::lock_guard<std::mutex> guard(mutex);
        if (!closing && callback) callback(kind, value.c_str());
    }
};
struct Activator : implements<Activator, INotificationActivationCallback> {
    std::shared_ptr<Shared> shared;
    explicit Activator(std::shared_ptr<Shared> value) : shared(std::move(value)) {}
    HRESULT __stdcall Activate(LPCWSTR, LPCWSTR arguments, const NOTIFICATION_USER_INPUT_DATA*, ULONG) noexcept override {
        try { shared->emit(1, arguments ? arguments : L""); return S_OK; }
        catch (...) { return E_FAIL; }
    }
};
struct Factory : implements<Factory, IClassFactory> {
    std::shared_ptr<Shared> shared;
    explicit Factory(std::shared_ptr<Shared> value) : shared(std::move(value)) {}
    HRESULT __stdcall CreateInstance(IUnknown* outer, REFIID iid, void** result) noexcept override {
        if (outer) return CLASS_E_NOAGGREGATION;
        try { return make_self<Activator>(shared)->QueryInterface(iid, result); }
        catch (...) { return E_FAIL; }
    }
    HRESULT __stdcall LockServer(BOOL) noexcept override { return S_OK; }
};
struct Context {
    Apartment apartment; // Declared first: release COM objects before uninitializing.
    hstring id;
    ToastNotifier notifier{nullptr};
    std::shared_ptr<Shared> shared;
    std::map<std::wstring, Delivery> deliveries;
    std::deque<std::wstring> order;
    DWORD factory_cookie{};
    ~Context() { if (factory_cookie) CoRevokeClassObject(factory_cookie); }
};
static HRESULT failure() noexcept {
    try { throw; }
    catch (hresult_error const& error) { last_error = error.message().c_str(); return error.code(); }
    catch (...) { last_error = L"Native notification operation failed"; return E_FAIL; }
}
#define API extern "C" __declspec(dllexport)
API wchar_t const* __stdcall dc_error() noexcept { return last_error.c_str(); }

API HRESULT __stdcall dc_shortcut(wchar_t const* path, wchar_t const* target,
                                  wchar_t const* arguments, wchar_t const* icon, wchar_t const* id,
                                  wchar_t const* activator) noexcept {
    try {
        Apartment apartment;
        auto link = create_instance<IShellLinkW>(CLSID_ShellLink);
        // Never replace an unrelated existing shortcut.
        if (GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES) {
            check_hresult(link.as<IPersistFile>()->Load(path, STGM_READ));
            PROPVARIANT value{};
            check_hresult(link.as<IPropertyStore>()->GetValue(PKEY_AppUserModel_ID, &value));
            bool ours = value.vt == VT_LPWSTR && value.pwszVal && wcscmp(value.pwszVal, id) == 0;
            PropVariantClear(&value);
            if (!ours) throw hresult_access_denied();
        }
        check_hresult(link->SetPath(target));
        check_hresult(link->SetArguments(arguments));
        check_hresult(link->SetIconLocation(icon, 0));
        PROPVARIANT value{};
        check_hresult(InitPropVariantFromString(id, &value));
        auto properties = link.as<IPropertyStore>();
        auto result = properties->SetValue(PKEY_AppUserModel_ID, value);
        PropVariantClear(&value);
        check_hresult(result);
        GUID clsid{};
        check_hresult(CLSIDFromString(activator, &clsid));
        PROPVARIANT activation{};
        activation.vt = VT_CLSID;
        activation.puuid = &clsid; // borrowed until SetValue returns
        check_hresult(properties->SetValue(PKEY_AppUserModel_ToastActivatorCLSID, activation));
        check_hresult(properties->Commit());
        check_hresult(link.as<IPersistFile>()->Save(path, TRUE));
        return S_OK;
    } catch (...) { return failure(); }
}

API HRESULT __stdcall dc_open(wchar_t const* id, wchar_t const* activator, Callback callback, void** output) noexcept {
    if (!id || !callback || !output) return E_INVALIDARG;
    *output = nullptr;
    try {
        auto context = std::make_unique<Context>();
        context->id = hstring(id);
        context->shared = std::make_shared<Shared>();
        context->shared->callback = callback;
        GUID clsid{};
        check_hresult(CLSIDFromString(activator, &clsid));
        auto factory = make_self<Factory>(context->shared);
        check_hresult(CoRegisterClassObject(clsid, factory.get(), CLSCTX_LOCAL_SERVER,
                                           REGCLS_MULTIPLEUSE, &context->factory_cookie));
        context->notifier = ToastNotificationManager::CreateToastNotifier(context->id);
        *output = context.release();
        return S_OK;
    } catch (...) { return failure(); }
}

API HRESULT __stdcall dc_setting(void* handle, int* value) noexcept {
    try { *value = static_cast<int>(static_cast<Context*>(handle)->notifier.Setting()); return S_OK; }
    catch (...) { return failure(); }
}

// Copy identity: callers may pass a key owned by the very map/deque erased here.
static void revoke(Context* context, std::wstring tag) {
    auto entry = context->deliveries.find(tag);
    if (entry == context->deliveries.end()) return;
    auto& delivery = entry->second;
    delivery.toast.Activated(delivery.activated);
    delivery.toast.Failed(delivery.failed);
    delivery.toast.Dismissed(delivery.dismissed);
    context->deliveries.erase(entry);
    std::erase(context->order, tag);
}

API HRESULT __stdcall dc_show(void* handle, wchar_t const* tag, wchar_t const* xml) noexcept {
    try {
        auto context = static_cast<Context*>(handle);
        // Windows itself enforces notification policy. Unknown Setting errors
        // are surfaced to Python, never interpreted as an Enabled status.
        revoke(context, tag);
        // COM activation handles historical toasts independently of the live
        // ToastNotification objects. Bound subscriptions, not total deliveries.
        while (context->deliveries.size() >= 64) revoke(context, context->order.front());
        XmlDocument document;
        document.LoadXml(xml);
        Delivery delivery;
        delivery.toast = ToastNotification(document);
        delivery.toast.Tag(tag);
        delivery.toast.Group(L"calendar-reminders");
        auto shared = context->shared;
        std::wstring identity(tag);
        delivery.activated = delivery.toast.Activated([shared](auto const&, Windows::Foundation::IInspectable const& args) {
            try { shared->emit(1, std::wstring(args.as<ToastActivatedEventArgs>().Arguments())); }
            catch (...) { shared->emit(2, L"activation-arguments-unavailable"); }
        });
        delivery.failed = delivery.toast.Failed([shared, identity](auto const&, ToastFailedEventArgs const& args) {
            shared->emit(2, identity + L":" + std::to_wstring(args.ErrorCode().value));
        });
        delivery.dismissed = delivery.toast.Dismissed([shared, identity](auto const&, ToastDismissedEventArgs const&) {
            shared->emit(3, identity);
        });
        context->deliveries.emplace(identity, delivery);
        context->order.push_back(identity);
        context->notifier.Show(delivery.toast);
        return S_OK;
    } catch (...) { return failure(); }
}

API HRESULT __stdcall dc_remove(void* handle, wchar_t const* tag) noexcept {
    try {
        auto context = static_cast<Context*>(handle);
        ToastNotificationManager::History().Remove(tag, L"calendar-reminders", context->id);
        revoke(context, tag);
        return S_OK;
    } catch (...) { return failure(); }
}

API HRESULT __stdcall dc_count(void* handle, int* value) noexcept {
    try {
        *value = static_cast<int>(ToastNotificationManager::History().GetHistory(static_cast<Context*>(handle)->id).Size());
        return S_OK;
    } catch (...) { return failure(); }
}

// Thread-safe callback gate only; no COM calls. Allows shutdown to disable
// Python callbacks immediately even if the owner MTA is finishing an RPC.
API void __stdcall dc_silence(void* handle) noexcept {
    if (!handle) return;
    auto shared = static_cast<Context*>(handle)->shared;
    std::lock_guard<std::mutex> guard(shared->mutex);
    shared->closing = true;
    shared->callback = nullptr;
}

API HRESULT __stdcall dc_close(void* handle) noexcept {
    if (!handle) return S_OK;
    auto context = std::unique_ptr<Context>(static_cast<Context*>(handle));
    { std::lock_guard<std::mutex> guard(context->shared->mutex);
      context->shared->closing = true; context->shared->callback = nullptr; }
    try {
        ToastNotificationManager::History().RemoveGroup(L"calendar-reminders", context->id);
        while (!context->deliveries.empty()) revoke(context.get(), context->deliveries.begin()->first);
        return S_OK;
    } catch (...) { return failure(); }
}
