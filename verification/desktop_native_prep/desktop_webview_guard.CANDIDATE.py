"""CANDIDATE: recovered origin guard plus pinned-SDK canonical COM readback repair.

Native wrapper identity observed in run45545155278. Candidate behavior remains
unqualified until its own native popup/cleanup/pixels pass.
Source-only WebView2 main-frame origin guard for exactly pywebview 5.4.

Native behavior has not been run on Windows. See README.md and
WINDOWS_ACCEPTANCE.md. This is not a network, iframe or download-origin filter.
Create the host with url=None, html=None and no startup script. Call start()
from webview.start(func=...) so worker failures get the fatal UI path.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger("hony.webview2_origin_guard")
SUPPORTED_PYWEBVIEW_VERSION = "5.4"
EXPECTED_RENDERER = "edgechromium"
INSTALL_TIMEOUT_SECONDS = 20.0
_BACKEND_POLL_SECONDS = 0.05
def native_window_text(kind: str, language: str | None = None) -> tuple[str, str]:
    import locale
    language = language or (locale.getlocale()[0] or 'ar').split('_')[0].split('-')[0]
    texts = {
        'ar': {
            'fatal': ('تعذر تأمين نافذة التطبيق ويجب إغلاقها. لن يتم تشغيل نافذة غير محمية. تواصل مع الدعم.', 'خطأ في أمان التطبيق'),
            'popup': ('تعذر فتح نافذة المستند وأُغلقت. صفحة التعديل لم تُستبدل. حاول مرة أخرى أو تواصل مع الدعم.', 'نافذة المستند'),
            'document': ('', 'المستند'),
        },
        'en': {
            'fatal': ('The application window could not be secured and must close. No unguarded startup will be attempted. Contact support.', 'Application security error'),
            'popup': ('The document window could not open and was closed. Your editor was not replaced. Try again or contact support.', 'Document window'),
            'document': ('', 'Document'),
        },
        'zh': {
            'fatal': ('无法保护应用窗口，必须关闭。不会启动未受保护的窗口。请联系支持人员。', '应用安全错误'),
            'popup': ('无法打开文档窗口，该窗口已关闭。编辑页面未被替换。请重试或联系支持人员。', '文档窗口'),
            'document': ('', '文档'),
        },
    }
    return texts.get(language, texts['ar'])[kind]



class GuardInstallError(RuntimeError):
    """The boundary is unavailable. Never continue or retry in this host."""


@dataclass(frozen=True)
class CanonicalOrigin:
    host: str
    port: int

    @property
    def authority(self) -> str:
        return self.host if self.port == 443 else f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"https://{self.authority}"


@dataclass(frozen=True)
class NavigationDecision:
    allowed: bool
    reason: str


def _unsafe_url_text(value: str) -> bool:
    # Avoid urllib/Chromium disagreement over whitespace and backslashes.
    return "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value)


def canonicalize_origin(origin: str) -> CanonicalOrigin:
    """Freeze an exact lowercase ASCII HTTPS host and effective port."""
    if not isinstance(origin, str) or not origin or _unsafe_url_text(origin):
        raise ValueError("origin must be a nonempty canonical URL")
    try:
        parts = urlsplit(origin)
        host = parts.hostname or ""
        port = parts.port
        host.encode("ascii")
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("origin is not parseable ASCII") from exc
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        raise ValueError("origin must be HTTPS without credentials")
    if parts.path or parts.query or parts.fragment:
        raise ValueError("origin must not contain path, query or fragment")
    if not host or host != host.lower() or host.endswith("."):
        raise ValueError("origin host must be lowercase without a trailing dot")
    if port == 443:
        raise ValueError("omit the default HTTPS port")
    effective_port = 443 if port is None else port
    if not 1 <= effective_port <= 65535:
        raise ValueError("origin port out of range")
    expected = f"https://{host}" + (f":{port}" if port is not None else "")
    if origin != expected:
        raise ValueError("origin must be in exact canonical form")
    return CanonicalOrigin(host, effective_port)


def decide_navigation(uri: str | None, origin: CanonicalOrigin) -> NavigationDecision:
    """Decide main-frame HTTPS navigation. Does not authorize subresources."""
    if not isinstance(uri, str) or not uri:
        return NavigationDecision(False, "empty-or-missing-uri")
    if _unsafe_url_text(uri):
        return NavigationDecision(False, "unsafe-url-text")
    try:
        parts = urlsplit(uri)
        host = parts.hostname or ""
        port = parts.port
        host.encode("ascii")
    except (ValueError, UnicodeEncodeError):
        return NavigationDecision(False, "malformed-uri")
    if parts.scheme != "https":
        return NavigationDecision(False, "scheme-not-https")
    if parts.username is not None or parts.password is not None:
        return NavigationDecision(False, "credentials-in-uri")
    if host != origin.host:
        return NavigationDecision(False, "origin-host-mismatch")
    if (443 if port is None else port) != origin.port:
        return NavigationDecision(False, "origin-port-mismatch")
    return NavigationDecision(True, "same-origin-https")


class WebView2OriginGuard:
    """One guard per host. Any fatal error permanently poisons this instance.

    _fatal_shutdown is a test seam, not a production recovery callback. Its
    production implementation disposes the view, reports a native error and
    terminates this dedicated desktop process. Do not override it in the app.
    """

    def __init__(self, origin: str, *, install_timeout: float = INSTALL_TIMEOUT_SECONDS):
        self.origin = canonicalize_origin(origin)
        self.install_timeout = install_timeout
        self.allowed_navigations = 0
        self.blocked_navigations = 0
        self.blocked_new_windows = 0
        self.opened_new_windows = 0
        self._popups = set()
        self._popup_factory = _NativeGuardedPopup
        self._root_guard = self
        self._lock = threading.RLock()
        self._settled = threading.Event()
        self._ready = threading.Event()  # success only, never an error wakeup
        self._failure: str | None = None
        self._attached = False
        self._fatal_started = False
        self._window = None
        self._control = None
        self._backend = None

    def _read_environment(self) -> tuple[str, str]:
        import importlib.metadata
        import webview
        return importlib.metadata.version("pywebview"), webview.renderer

    def _verify_environment(self) -> None:
        version, renderer = self._read_environment()
        if version != SUPPORTED_PYWEBVIEW_VERSION:
            raise GuardInstallError(f"unsupported-pywebview-version:{version}")
        if renderer != EXPECTED_RENDERER:
            raise GuardInstallError(f"unsupported-renderer:{renderer}")

    @staticmethod
    def _wait_for_backend(window, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            native = getattr(window, "native", None)
            browser = getattr(native, "browser", None)
            if getattr(browser, "webview", None) is not None:
                return browser
            time.sleep(_BACKEND_POLL_SECONDS)
        raise GuardInstallError("native-webview2-backend-unavailable")

    @staticmethod
    def _marshal_to_ui(control, callback) -> None:
        if control.InvokeRequired:
            from System import Action
            control.Invoke(Action(callback))
        else:
            callback()

    def install(self, window, *, timeout: float | None = None) -> "WebView2OriginGuard":
        """Install before remote loading. Failure invokes fatal shutdown.

        Direct callers also receive GuardInstallError if the shutdown test
        seam returns. Production shutdown does not return.
        """
        self._window = window
        timeout = self.install_timeout if timeout is None else timeout
        try:
            self._verify_environment()
            backend = self._wait_for_backend(window, timeout)
            with self._lock:
                if self._failure:
                    raise GuardInstallError(self._failure)
                self._backend = backend
                self._control = backend.webview

            def subscribe_and_check():
                with self._lock:
                    if self._failure or self._attached:
                        return
                    self._control.CoreWebView2InitializationCompleted += self._on_core_initialization_completed
                    self._attach_if_ready()

            self._marshal_to_ui(self._control, subscribe_and_check)
            if not self._settled.wait(timeout):
                raise GuardInstallError("install-timeout")
            self.assert_ready()
            return self
        except Exception as exc:
            reason = str(exc) if isinstance(exc, GuardInstallError) else "install-exception"
            self._fatal(reason)
            raise GuardInstallError(self._failure or reason) from exc

    def _on_core_initialization_completed(self, sender, args) -> None:
        try:
            if not args.IsSuccess:
                self._fatal("webview2-initialization-failed")
                return
            self._attach_if_ready()
        except Exception:  # noqa: BLE001 - native failures must close the affected view
            self._fatal("initialization-callback-failed")

    def _attach_if_ready(self) -> None:
        with self._lock:
            if self._attached or self._failure:
                return
            try:
                core = self._control.CoreWebView2
                if core is None:
                    return
                # Under the pinned 5.4 contract its completion handler was
                # subscribed first. The UI invocation cannot interleave it.
                # A removal exception is fatal, NEVER treated as absence.
                try:
                    core.NewWindowRequested -= self._backend.on_new_window_request
                except Exception:  # noqa: BLE001 - native failures must close the affected view
                    self._fatal("default-new-window-handler-removal-failed")
                    return
                self._control.NavigationStarting += self.on_navigation_starting
                core.NewWindowRequested += self.on_new_window_requested
                core.DownloadStarting -= self._backend.on_download_starting
                core.DownloadStarting += self.on_download_starting
            except Exception:  # noqa: BLE001 - native failures must close the affected view
                # Keep any installed guard in place until host disposal.
                # Removing guards as rollback would reopen the boundary.
                self._fatal("hook-attach-failed")
                return
            self._attached = True
            self._ready.set()
            self._settled.set()
            logger.info("WebView2 origin guard active for %s", self.origin.url)

    def on_download_starting(self, sender, args) -> None:
        # The pinned backend shows a modal SaveFileDialog synchronously. Hold
        # WebView2 completion and post it out of the callback's nested loop.
        # Reuse that exact picker, never fetch data or choose a destination.
        try:
            deferral = args.GetDeferral()
            args.Handled = True
        except Exception:
            try:args.Cancel = True
            finally:self._fatal("download-deferral-failed")
            return
        def choose():
            try:
                self._backend.on_download_starting(sender, args)
            except Exception:
                args.Cancel = True
                self._fatal("download-picker-failed")
            finally:
                try:deferral.Complete()
                except Exception:self._fatal("download-deferral-completion-failed")
        try:
            from System import Action
            self._control.BeginInvoke(Action(choose))
        except Exception:
            try:args.Cancel = True
            finally:
                try:deferral.Complete()
                finally:self._fatal("download-picker-post-failed")

    def _fatal(self, reason: str) -> None:
        with self._lock:
            if self._fatal_started:
                return
            self._fatal_started = True
            self._failure = reason
            self._attached = False
            self._ready.clear()
            self._settled.set()
        # No raw URI, native exception text or query/session data in logs/UI.
        logger.critical("WebView2 origin guard fatal: %s", reason)
        self._fatal_shutdown()

    def _fatal_shutdown(self) -> None:
        """Synchronous containment, visible native error, process termination.

        Dispose, not window.destroy()/Close(), avoids a closing-event veto.
        If disposal/marshalling fails, exit immediately rather than leaving a
        live view behind a modal dialog. Dedicated-process integration only.
        """
        import os
        try:
            self._dispose_popup_controls()
            if self._control is not None:
                def dispose():
                    self._control.Dispose()
                    if not self._control.IsDisposed:
                        raise RuntimeError("view-not-disposed")
                self._marshal_to_ui(self._control, dispose)
            # A backend-less startup has never been allowed a remote URL.
            self._show_fatal_message()
        except BaseException:  # noqa: BLE001 - last-resort dedicated-process containment
            logger.critical("Fatal UI containment/reporting failed; terminating")
        finally:
            os._exit(1)

    def _dispose_popup_controls(self):
        # Do not leave auxiliary documents alive behind a fatal modal dialog.
        for popup in list(self._popups):
            if popup.guard is not None:
                popup.guard._dispose_popup_controls()
            if popup.control is not None:
                def dispose_popup(popup=popup):
                    popup.control.Dispose()
                    if not popup.control.IsDisposed:
                        raise RuntimeError("popup-not-disposed")
                self._marshal_to_ui(popup.control, dispose_popup)

    @staticmethod
    def _show_fatal_message() -> None:
        import ctypes
        # Worker-safe Win32 dialog. Does not depend on a pywebview JS bridge
        # or an initialized CoreWebView2. Return only after acknowledgement.
        message, title = native_window_text("fatal")
        result = ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 | 0x1000)
        if result == 0:
            raise RuntimeError("fatal-dialog-failed")

    def assert_ready(self) -> None:
        if self._failure or not self._attached or not self._ready.is_set():
            raise GuardInstallError(self._failure or "guard-not-installed")

    @staticmethod
    def _read_uri(args) -> str | None:
        try:
            raw = args.Uri
            return None if raw is None else str(raw)
        except Exception:  # noqa: BLE001 - native failures must close the affected view
            return None

    def _set_true(self, args, name: str) -> bool:
        # Require readback too: a silently ignored write is not containment.
        for use_accessor in (False, True):
            try:
                if use_accessor:
                    getattr(args, "set_" + name)(True)
                else:
                    setattr(args, name, True)
                if bool(getattr(args, name)):
                    return True
            except Exception:  # noqa: BLE001 - try alternate native setter without raw exception logging
                pass  # noqa: S110 - fallback setter/readback immediately follows
        self._fatal("cannot-set-" + name.lower())
        return False

    def on_navigation_starting(self, sender, args) -> None:
        decision = decide_navigation(self._read_uri(args), self.origin)
        if decision.allowed and not self._failure:
            self.allowed_navigations += 1
            return
        self.blocked_navigations += 1
        self._set_true(args, "Cancel")

    def on_new_window_requested(self, sender, args) -> None:
        # Always suppress default popup creation before creating a guarded view.
        # Never call pywebview's default opener or an external browser.
        target = self._read_uri(args)
        if not self._set_true(args, "Handled"):
            return
        if self._failure:
            return
        decision = decide_navigation(target, self.origin)
        if not decision.allowed:
            self.blocked_new_windows += 1
            return
        popup = None
        try:
            self.assert_ready()
            # Keep the original native request (including its authentication
            # and request semantics). Never load its URI into the editor.
            popup = self._popup_factory(self, sender, args)
            self._popups.add(popup)  # retain Python delegates and native owner
            popup.begin()
        except Exception:  # noqa: BLE001 - native failures must close the affected view
            if popup is not None:
                popup.fail("popup-creation-failed")
            else:
                try:
                    self._report_popup_failure()
                except Exception:  # noqa: BLE001 - native failures must close the affected view
                    logger.error("Separate document view error dialog unavailable")

    @staticmethod
    def _report_popup_failure() -> None:
        import ctypes
        # Recoverable viewer errors leave the editor and unsaved work intact.
        message, title = native_window_text("popup")
        result = ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 | 0x1000)
        if result == 0:
            raise RuntimeError("popup-dialog-failed")

    @property
    def start_url(self) -> str:
        return self.origin.url + "/"

    def load_start_url(self, window, url: str | None = None) -> None:
        self.assert_ready()
        target = self.start_url if url is None else url
        decision = decide_navigation(target, self.origin)
        if not decision.allowed:
            raise GuardInstallError(f"start-url-rejected:{decision.reason}")
        window.load_url(target)

    def start(self, window, url: str | None = None) -> bool:
        """Worker entrypoint: no startup exception is lost in the thread."""
        self._window = window
        try:
            self.install(window)
            self.load_start_url(window, url)
            return True
        except Exception:  # noqa: BLE001 - native failures must close the affected view
            self._fatal("guarded-startup-failed")
            return False  # only reachable with the non-exiting test seam


def _same_native_webview(left, right):
    """Fail closed on any SDK/type/field change. Never compare wrapper addresses."""
    if left is None or right is None:
        raise GuardInstallError('popup-null-readback')
    from System import IntPtr
    from System.Reflection import BindingFlags
    from System.Runtime.InteropServices import Marshal
    pointers = []
    try:
        for value in (left, right):
            kind = value.GetType()
            if (str(kind.FullName) != 'Microsoft.Web.WebView2.Core.CoreWebView2' or
                    str(kind.Assembly.GetName().Version) != '1.0.2957.106'):
                raise GuardInstallError('popup-unqualified-SDK-identity')
            field = kind.GetField('_rawNative', BindingFlags.Instance | BindingFlags.NonPublic)
            if field is None or str(field.FieldType.FullName) != 'System.Object':
                raise GuardInstallError('popup-native-identity-field-unavailable')
            raw = field.GetValue(value)
            if raw is None or not Marshal.IsComObject(raw):
                raise GuardInstallError('popup-native-identity-not-COM')
            pointer = Marshal.GetIUnknownForObject(raw)
            pointers.append(pointer)
            if pointer == IntPtr.Zero:
                raise GuardInstallError('popup-native-identity-null')
        return pointers[0] == pointers[1]
    finally:
        for pointer in reversed(pointers):
            if pointer != IntPtr.Zero:
                Marshal.Release(pointer)


class _NativeGuardedPopup:
    """UI-thread-only, un-navigated WebView2 supplied to the native request.

    No EdgeChrome constructor: that constructor initializes its own environment,
    loads a placeholder and installs an external opener. Here the opener's exact
    environment/profile is used and guards precede the NewWindow handoff.
    """

    def __init__(self, parent, opener, args):
        self.parent = parent
        self.opener = opener
        self.args = args
        self.deferral = None
        self.form = None
        self.control = None
        self.timer = None
        self.guard = None
        self.closed = False
        self.handed_off = False
        self._newwindow_assignment_attempted = False
        self._owner_hooked = False
        self._initialization_task = None

    def begin(self):
        # No synchronous wait on the native event/UI thread.
        self.deferral = self.args.GetDeferral()
        self._create_native()
        self.timer.Start()
        self._initialization_task = self.control.EnsureCoreWebView2Async(self.opener.Environment)

    def _create_native(self):
        import System.Windows.Forms as Forms
        from Microsoft.Web.WebView2.WinForms import WebView2, CoreWebView2CreationProperties
        self.form = Forms.Form()
        self.form.Text = native_window_text("document")[1]
        self.form.Width = 1000
        self.form.Height = 760
        self.form.ShowInTaskbar = False
        self.control = WebView2()
        props = CoreWebView2CreationProperties()
        props.ProfileName = self.opener.Profile.ProfileName
        props.set_IsInPrivateModeEnabled(self.opener.Profile.IsInPrivateModeEnabled)
        self.control.CreationProperties = props
        self.control.Dock = Forms.DockStyle.Fill
        self.control.DefaultBackgroundColor = self.parent._control.DefaultBackgroundColor
        self.form.Controls.Add(self.control)
        # Force a native handle while still hidden, before explicit init.
        _ = self.form.Handle
        _ = self.control.Handle
        self.control.CoreWebView2InitializationCompleted += self._on_initialized
        self.form.FormClosed += self._on_closed
        self.parent._backend.form.FormClosed += self._on_owner_closed
        self._owner_hooked = True
        self.timer = Forms.Timer()
        self.timer.Interval = max(1, int(self.parent.install_timeout * 1000))
        self.timer.Tick += self._on_timeout

    def _copy_settings(self, core):
        # Match 5.4's settings and do not touch shared PDF toolbar settings.
        source = self.opener.Settings
        target = core.Settings
        for name in (
            "AreBrowserAcceleratorKeysEnabled", "AreDefaultContextMenusEnabled",
            "AreDefaultScriptDialogsEnabled", "AreDevToolsEnabled",
            "IsBuiltInErrorPageEnabled", "IsScriptEnabled", "IsWebMessageEnabled",
            "IsStatusBarEnabled", "IsSwipeNavigationEnabled", "IsZoomControlEnabled",
            "UserAgent",
        ):
            setattr(target, name, getattr(source, name))

    def _on_initialized(self, sender, args):
        if self.closed or self.handed_off:
            return
        try:
            self.parent.assert_ready()
            if not args.IsSuccess:
                raise GuardInstallError("popup-initialization-failed")
            core = self.control.CoreWebView2
            if core is None:
                raise GuardInstallError("popup-core-unavailable")
            if (core.Profile.ProfileName != self.opener.Profile.ProfileName or
                    core.Profile.IsInPrivateModeEnabled != self.opener.Profile.IsInPrivateModeEnabled):
                raise GuardInstallError("popup-profile-mismatch")
            self._copy_settings(core)
            # A fresh control has no pywebview default opener to remove.
            # Its guard uses the same origin decision and setter containment.
            self.guard = WebView2OriginGuard(self.parent.origin.url,
                                            install_timeout=self.parent.install_timeout)
            self.guard._control = self.control
            self.guard._backend = self.parent._backend
            self.guard._popup_factory = self.parent._popup_factory
            self.guard._root_guard = self.parent._root_guard
            self.guard._fatal_shutdown = lambda: self.fail("popup-guard-failure")
            self.control.NavigationStarting += self._on_navigation
            self.control.NavigationCompleted += self._on_navigation_completed
            core.NewWindowRequested += self.guard.on_new_window_requested
            # Reuse the application's exact 5.4 download callback, not a new
            # file fetcher. It reads ALLOW_DOWNLOADS and uses its Save dialog.
            core.DownloadStarting += self.parent.on_download_starting
            core.WindowCloseRequested += self._on_close_requested
            core.ProcessFailed += self._on_process_failed
            self.guard._attached = True
            self.guard._ready.set()
            self.guard._settled.set()
            # No Source/load_url/NavigateToString before or after handoff.
            self._newwindow_assignment_attempted = True
            self.args.NewWindow = core
            if not _same_native_webview(self.args.NewWindow, core):
                raise GuardInstallError("popup-newwindow-readback-failed")
            self.handed_off = True
            self.parent.opened_new_windows += 1
            self.timer.Stop()
            self._complete_deferral()
            self.form.Show(self.parent._backend.form)
        except Exception:  # noqa: BLE001 - native failures must close the affected view
            self.fail("popup-initialization-or-handoff-failed")

    def _on_navigation(self, sender, args):
        if self.closed:
            self.guard._set_true(args, "Cancel")
            return
        self.guard.on_navigation_starting(sender, args)
        if not self.closed and not decide_navigation(self.guard._read_uri(args), self.parent.origin).allowed:
            self.fail("popup-navigation-rejected")

    def _on_navigation_completed(self, sender, args):
        if self.closed:
            return
        try:
            # Superseded requests can report OperationCanceled. Leave these
            # alone rather than close a view during its native Save dialog.
            # Exact download/navigation status sequences need Windows checks.
            if not args.IsSuccess and str(args.WebErrorStatus) != "OperationCanceled":
                self.fail("popup-navigation-failed")
        except Exception:  # noqa: BLE001 - native failures must close the affected view
            self.fail("popup-navigation-status-unavailable")

    def _on_timeout(self, sender, args):
        if not self.handed_off:
            self.fail("popup-initialization-timeout")

    def _on_process_failed(self, sender, args):
        self.fail("popup-process-failed")

    def _on_close_requested(self, sender, args):
        self.close()

    def _on_closed(self, sender, args):
        self.close()

    def _on_owner_closed(self, sender, args):
        self.close()

    def _complete_deferral(self):
        if self.deferral is not None:
            self.deferral.Complete()
            self.deferral = None

    def close(self):
        if self.closed:
            return
        self.closed = True  # late callbacks cannot revive this view
        if self.guard is not None:
            self.guard._failure = self.guard._failure or "document-view-closed"
            self.guard._attached = False
            self.guard._ready.clear()
        try:
            if self.timer is not None:
                self.timer.Stop()
                self.timer.Dispose()
            # Guard stays installed until the control has actually disposed.
            if self.control is not None:
                self.control.Dispose()
                if not self.control.IsDisposed:
                    raise GuardInstallError("popup-not-disposed")
            # NewWindow is immutable after assignment. A failed attempted
            # assignment/readback has uncertain native state: terminate the
            # dedicated window process after disposal, never mutate/retry it.
            if self._newwindow_assignment_attempted and not self.handed_off:
                raise GuardInstallError('popup-assignment-state-unconfirmed')
            self._complete_deferral()
            if self._owner_hooked:
                self.parent._backend.form.FormClosed -= self._on_owner_closed
            if self.form is not None:
                self.form.Dispose()
            # Any descendants are document views too, never the editor.
            if self.guard is not None:
                for child in list(self.guard._popups):
                    child.close()
            self.parent._popups.discard(self)
        except Exception:  # noqa: BLE001 - native failures must close the affected view
            # If local containment is unverifiable, retain the original fatal
            # process-stop policy. This exceptional path may lose editor work.
            self.parent._root_guard._fatal("popup-containment-failed")

    def fail(self, reason):
        if self.closed:
            return
        logger.warning("Separate document view closed: %s", reason)
        self.close()
        if not self.parent._failure:
            try:
                self.parent._report_popup_failure()
            except Exception:  # noqa: BLE001 - native failures must close the affected view
                # The unsafe view is already gone. Do not sacrifice editor
                # work solely because the recoverable error dialog failed.
                logger.error("Separate document view error dialog unavailable")
