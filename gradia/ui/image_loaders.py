# Copyright (C) 2025 Alexander Vanhee
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import mimetypes
import shutil
import subprocess
import time
import threading
from urllib.parse import urlparse, unquote
import urllib.request
import gi
from gi.repository import Gtk, Gio, Gdk, GLib, Xdp

try:
    gi.require_version('XdpGtk4', '1.0')
    from gi.repository import XdpGtk4
except Exception:
    XdpGtk4 = None
from gradia.clipboard import save_texture_to_file
from gradia.ui.image_creation.source_image_generator import SourceImageGeneratorWindow
from gradia.utils.timestamp_filename import TimestampedFilenameGenerator
from gradia.backend.logger import Logger
from gradia.graphics.loaded_image import LoadedImage, ImageOrigin
from typing import Optional, Callable
ImportFormat = tuple[str, str]

logger = Logger()

class BaseImageLoader:
    SUPPORTED_INPUT_FORMATS: list[ImportFormat] = [
        (".png", "image/png"),
        (".jpg", "image/jpg"),
        (".jpeg", "image/jpeg"),
        (".webp", "image/webp"),
        (".avif", "image/avif"),
    ]

    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str) -> None:
        self.window: Gtk.ApplicationWindow = window
        self.temp_dir: str = temp_dir

    def _is_supported_format(self, file_path: str) -> bool:
        lower_path = file_path.lower()
        supported_extensions = [ext for ext, _ in self.SUPPORTED_INPUT_FORMATS]
        return any(lower_path.endswith(ext) for ext in supported_extensions)

    def _set_image_and_update_ui(self, file_path: str, origin: ImageOrigin, screenshot_path: str = None, copy_after_processing: bool = False) -> None:
        self.window.show_loading_state()

        def load_image_thread():
            try:
                loaded_image = LoadedImage(file_path, origin, screenshot_path)
                GLib.idle_add(self._on_image_loaded, loaded_image, copy_after_processing)
            except Exception as e:
                logger.error(f"Error loading image in thread: {e}")
                GLib.idle_add(self._on_image_load_error, str(e))

        thread = threading.Thread(target=load_image_thread, daemon=True)
        thread.start()

    def _on_image_loaded(self, loaded_image: LoadedImage, copy_after_processing: bool) -> bool:
        self.window.set_image(loaded_image, copy_after_processing=copy_after_processing)
        return False

    def _on_image_load_error(self, error_message: str) -> bool:
        self.window._show_notification(f"Failed to load image: {error_message}")
        self.window.hide_loading_state()
        return False

    def _handle_uri(self, uri: str, origin: ImageOrigin) -> bool:
        logger.info(f"Processing URI: {uri}")

        if uri.startswith("file://"):
            file_path = unquote(urlparse(uri).path)

            if not os.path.isfile(file_path):
                logger.error("File does not exist:", file_path)
                return False

            if not self._is_supported_format(file_path):
                self.window._show_notification(_("Not a supported image format"))
                return False

            self._set_image_and_update_ui(file_path, origin)
            return True

        elif uri.startswith(("http://", "https://")):
            return self._handle_image_url(uri, origin)

        else:
            logger.info("Unsupported URI scheme:", uri)
            self.window._show_notification(_("Not a supported image format"))
            return False

    def _handle_image_url(self, url: str, origin: ImageOrigin) -> bool:
        try:
            path = urlparse(url).path
            mime_type, _unused = mimetypes.guess_type(path)
            logger.info(f"mime type from guess_type: {mime_type}")

            if not (mime_type and mime_type.startswith("image/")):
                lower_path = path.lower()
                supported_extensions = [ext for ext, _ in self.SUPPORTED_INPUT_FORMATS]
                if not any(lower_path.endswith(ext) for ext in supported_extensions):
                    self.window._show_notification(_("Not a supported image format"))
                    return False
                else:
                    logger.info("Fallback: file extension matches supported image format.")

            filename = os.path.basename(path) or "downloaded_image"
            temp_path = os.path.join(self.temp_dir, filename)

            urllib.request.urlretrieve(url, temp_path)

            if not self._is_supported_format(temp_path):
                self.window._show_notification(_("URL is not a supported image format"))
                os.remove(temp_path)
                return False

            self._set_image_and_update_ui(temp_path, origin)
            return True

        except Exception as e:
            logger.error("Error downloading image:", e)
            self.window._show_notification(_("Failed to load image from URL."))
            return False

class FileDialogImageLoader(BaseImageLoader):
    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str) -> None:
        super().__init__(window, temp_dir)

    def open_file_dialog(self) -> None:
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title(_("Open Image"))

        image_filter = Gtk.FileFilter()
        image_filter.set_name(_("Image Files"))
        for _ext, mime_type in self.SUPPORTED_INPUT_FORMATS:
            image_filter.add_mime_type(mime_type)

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)
        file_dialog.set_filters(filters)

        file_dialog.open(self.window, None, self._on_file_selected)

    def _on_file_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            file = dialog.open_finish(result)
            if not file:
                return

            file_path = file.get_path()
            if not file_path or not os.path.isfile(file_path):
                logger.info(f"Invalid file path: {file_path}")
                return

            if not self._is_supported_format(file_path):
                logger.info(f"Unsupported file format: {file_path}")
                return

            self._set_image_and_update_ui(file_path, ImageOrigin.FileDialog)

        except Exception as e:
            logger.error(f"Error opening file: {e}")


class DragDropImageLoader(BaseImageLoader):
    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str) -> None:
        super().__init__(window, temp_dir)

    def handle_file_drop(
        self,
        drop_target: Optional[object],
        value: object,
        x: int,
        y: int
    ) -> bool:

        if isinstance(value, Gio.File):
            uri = value.get_uri()
            return self._handle_uri(uri, ImageOrigin.DragDrop)

        return False


class ClipboardImageLoader(BaseImageLoader):
    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str) -> None:
        super().__init__(window, temp_dir)

    def load_from_clipboard(self) -> None:
        display = Gdk.Display.get_default()
        clipboard = display.get_clipboard()

        clipboard.read_value_async(
            Gdk.FileList,
            0,
            None,
            self._on_uris_get
        )

    def _on_uris_get(self, clipboard, result, user_data=None) -> None:
        try:
            file_list = clipboard.read_value_finish(result)
            if file_list:
                files = file_list.get_files()
                if files:
                    file_obj = files[0]
                    uri = file_obj.get_uri()
                    self._handle_uri(uri, ImageOrigin.Clipboard)
        except GLib.GError as e:
            print(f"Error reading URIs: {e}")


class ScreenshotImageLoader(BaseImageLoader):
    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str, app: Gtk.Application) -> None:
        super().__init__(window, temp_dir)
        self.portal = Xdp.Portal()
        self._error_callback: Optional[Callable[[str], None]] = None
        self._success_callback: Optional[Callable[[], None]] = None
        self._screenshot_uris: list[str] = []
        self.window = window
        self._hide_signal_id: Optional[int] = None
        self._hide_timeout_id: Optional[int] = None
        self._pending_flags: Xdp.ScreenshotFlags = Xdp.ScreenshotFlags.INTERACTIVE
        self._fallback_proc: Optional[subprocess.Popen] = None
        self._fallback_path: Optional[str] = None
        self._fallback_timeout_id: Optional[int] = None
        self._fallback_start_time: float = 0.0
        self._niri_finished: bool = False
        self._niri_stream_proc: Optional[subprocess.Popen] = None

    def _update_delete_action_state(self) -> None:
        action = self.window.lookup_action("delete-screenshots")
        if action:
            action.set_enabled(bool(self._screenshot_uris))

    def take_screenshot(
        self,
        flags: Xdp.ScreenshotFlags = Xdp.ScreenshotFlags.INTERACTIVE,
        on_error_or_cancel: Optional[Callable[[str], None]] = None,
        on_success: Optional[Callable[[], None]] = None
    ) -> None:
        try:
            self._error_callback = on_error_or_cancel
            self._success_callback = on_success
            self._pending_flags = flags
            self._hide_signal_id = self.window.connect("hide", self._on_window_hidden)
            self._hide_timeout_id = GLib.timeout_add(500, self._on_hide_timeout, flags)
            self.window.hide()
        except Exception as e:
            logger.error(f"Failed to initiate screenshot: {e}")
            self.window._show_notification(_("Failed to take screenshot"))
            if on_error_or_cancel:
                on_error_or_cancel(str(e))

    def _on_window_hidden(self, window) -> None:
        if self._hide_signal_id:
            self.window.disconnect(self._hide_signal_id)
            self._hide_signal_id = None
        if self._hide_timeout_id:
            GLib.source_remove(self._hide_timeout_id)
            self._hide_timeout_id = None
        self._do_take_screenshot(self._pending_flags)

    def _on_hide_timeout(self, flags: Xdp.ScreenshotFlags) -> bool:
        if self._hide_signal_id:
            self.window.disconnect(self._hide_signal_id)
            self._hide_signal_id = None
        self._hide_timeout_id = None
        self._do_take_screenshot(flags)
        return False

    def _do_take_screenshot(self, flags: Xdp.ScreenshotFlags) -> bool:
        try:
            parent = None
            if XdpGtk4 and hasattr(XdpGtk4, 'parent_new_gtk') and self.window:
                try:
                    parent = XdpGtk4.parent_new_gtk(self.window)
                except Exception as e:
                    logger.warning(f"Failed to create GTK parent for portal: {e}")
            self.portal.take_screenshot(
                parent,
                flags,
                None,
                self._on_screenshot_taken,
                None
            )
        except Exception as e:
            logger.warning(f"Failed during portal screenshot initiation: {e}. Trying CLI fallback...")
            self._try_fallback_screenshot(flags)
        return False

    def _on_screenshot_taken(self, portal_object, result, user_data) -> None:
        try:
            uri = self.portal.take_screenshot_finish(result)
            self._screenshot_uris.append(uri)
            self._handle_screenshot_uri(uri)
            self._update_delete_action_state()
            self.window.show()
            self._error_callback = None
        except Exception as e:
            logger.warning(f"Portal screenshot failed or cancelled: {e}. Trying CLI fallback...")
            self._try_fallback_screenshot(self._pending_flags)

    def _try_fallback_screenshot(self, flags: Xdp.ScreenshotFlags) -> None:
        if self._fallback_timeout_id:
            GLib.source_remove(self._fallback_timeout_id)
            self._fallback_timeout_id = None

        if self._niri_stream_proc:
            try:
                self._niri_stream_proc.kill()
            except Exception:
                pass
            self._niri_stream_proc = None
        self._niri_finished = False

        filename = TimestampedFilenameGenerator().generate(_("Edited Screenshot From %Y-%m-%d %H-%M-%S")) + ".png"
        in_flatpak = os.path.exists("/.flatpak-info")
        if in_flatpak:
            pictures_dir = os.path.realpath(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~/Pictures"))
            base_dir = os.path.join(pictures_dir, ".gradia_temp")
            os.makedirs(base_dir, exist_ok=True)
            temp_path = os.path.abspath(os.path.join(base_dir, filename))
        else:
            temp_path = os.path.abspath(os.path.join(self.temp_dir, filename))

        is_interactive = (flags == Xdp.ScreenshotFlags.INTERACTIVE)
        cmd = self._get_fallback_screenshot_cmd(temp_path, is_interactive)

        if not cmd:
            logger.error("No suitable screenshot tool found for fallback.")
            self.window._show_notification(_("Failed to take screenshot"))
            self.window.show()
            if self._error_callback:
                self._error_callback("No screenshot tool available")
            self._error_callback = None
            return

        try:
            logger.info(f"Executing fallback screenshot command: {cmd}")
            self._fallback_proc = subprocess.Popen(cmd)
            self._fallback_path = temp_path
            self._fallback_start_time = time.time()

            if "niri" in cmd or (len(cmd) > 1 and "niri" in cmd[1]):
                self._start_niri_monitor(in_flatpak)

            self._fallback_timeout_id = GLib.timeout_add(150, self._check_fallback_status)
        except Exception as e:
            logger.error(f"Failed to launch fallback screenshot process: {e}")
            self.window._show_notification(_("Failed to take screenshot"))
            self.window.show()
            if self._error_callback:
                self._error_callback(str(e))
            self._error_callback = None

    def _start_niri_monitor(self, in_flatpak: bool) -> None:
        stream_cmd = ["niri", "msg", "event-stream"]
        if in_flatpak:
            stream_cmd = ["flatpak-spawn", "--host"] + stream_cmd

        def watch_events():
            try:
                proc = subprocess.Popen(stream_cmd, stdout=subprocess.PIPE, text=True)
                self._niri_stream_proc = proc
                focus_lost = False
                for line in proc.stdout:
                    l = line.strip()
                    if "Window focus changed: None" in l or "Overview toggled: true" in l:
                        focus_lost = True
                    elif focus_lost and ("Window focus changed: Some" in l or "Overview toggled: false" in l):
                        time.sleep(0.1)
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        self._niri_finished = True
                        break
            except Exception as e:
                logger.warning(f"Error monitoring Niri events: {e}")

        threading.Thread(target=watch_events, daemon=True).start()

    def _get_fallback_screenshot_cmd(self, target_path: str, is_interactive: bool) -> Optional[list[str]]:
        in_flatpak = os.path.exists("/.flatpak-info")

        def check_cmd(cmd_name: str) -> bool:
            if shutil.which(cmd_name):
                return True
            if in_flatpak:
                try:
                    res = subprocess.run(["flatpak-spawn", "--host", "which", cmd_name], capture_output=True)
                    return res.returncode == 0
                except Exception:
                    return False
            return False

        def wrap_cmd(cmd_args: list[str]) -> list[str]:
            if in_flatpak and not shutil.which(cmd_args[0]):
                return ["flatpak-spawn", "--host"] + cmd_args
            return cmd_args

        # 1. Niri (Wayland scrollable compositor)
        if check_cmd("niri"):
            action = "screenshot" if is_interactive else "screenshot-screen"
            return wrap_cmd(["niri", "msg", "action", action, "--path", target_path])

        # 2. Hyprshot (Hyprland)
        if check_cmd("hyprshot"):
            mode = "region" if is_interactive else "output"
            out_dir = os.path.dirname(target_path)
            out_file = os.path.basename(target_path)
            return wrap_cmd(["hyprshot", "-m", mode, "-o", out_dir, "-f", out_file, "--silent"])

        # 3. Grimshot (Sway/Wayland)
        if check_cmd("grimshot"):
            mode = "area" if is_interactive else "output"
            return wrap_cmd(["grimshot", "save", mode, target_path])

        # 4. Grim (+ Slurp for region)
        if check_cmd("grim"):
            if is_interactive and check_cmd("slurp"):
                if in_flatpak and not shutil.which("grim"):
                    return ["flatpak-spawn", "--host", "sh", "-c", f'grim -g "$(slurp)" "{target_path}"']
                return ["sh", "-c", f'grim -g "$(slurp)" "{target_path}"']
            elif not is_interactive:
                return wrap_cmd(["grim", target_path])

        # 5. GNOME Screenshot
        if check_cmd("gnome-screenshot"):
            flags = ["-a", "-f", target_path] if is_interactive else ["-f", target_path]
            return wrap_cmd(["gnome-screenshot"] + flags)

        # 6. Spectacle (KDE)
        if check_cmd("spectacle"):
            mode = "-r" if is_interactive else "-f"
            return wrap_cmd(["spectacle", "-b", mode, "-o", target_path])

        # 7. Flameshot
        if check_cmd("flameshot"):
            if is_interactive:
                if in_flatpak and not shutil.which("flameshot"):
                    return ["flatpak-spawn", "--host", "sh", "-c", f'flameshot gui -r > "{target_path}"']
                return ["sh", "-c", f'flameshot gui -r > "{target_path}"']
            else:
                return wrap_cmd(["flameshot", "full", "-p", target_path])

        # 8. XFCE Screenshooter
        if check_cmd("xfce4-screenshooter"):
            flags = ["-r", "-s", target_path] if is_interactive else ["-f", "-s", target_path]
            return wrap_cmd(["xfce4-screenshooter"] + flags)

        # 9. Scrot
        if check_cmd("scrot"):
            flags = ["-s", target_path] if is_interactive else [target_path]
            return wrap_cmd(["scrot"] + flags)

        # 10. Maim
        if check_cmd("maim"):
            flags = ["-s", target_path] if is_interactive else [target_path]
            return wrap_cmd(["maim"] + flags)

        # 11. ImageMagick Import (X11 only)
        if check_cmd("import") and os.environ.get("XDG_SESSION_TYPE") != "wayland":
            return wrap_cmd(["import", target_path])

        return None

    def _check_fallback_status(self) -> bool:
        target_path = self._fallback_path
        proc = self._fallback_proc

        def cleanup_niri_stream():
            if self._niri_stream_proc:
                try:
                    self._niri_stream_proc.kill()
                except Exception:
                    pass
                self._niri_stream_proc = None

        if target_path and os.path.exists(target_path) and os.path.getsize(target_path) > 0:
            logger.info(f"Fallback screenshot created {target_path}")
            self._fallback_timeout_id = None
            cleanup_niri_stream()
            self.window.show()
            try:
                uri = Gio.File.new_for_path(target_path).get_uri()
                self._screenshot_uris.append(uri)
                self._set_image_and_update_ui(target_path, ImageOrigin.Screenshot, screenshot_path=target_path, copy_after_processing=True)
                self.window._show_notification(_("Screenshot captured!"))
                self._update_delete_action_state()
                if self._success_callback:
                    self._success_callback()
            except Exception as e:
                logger.error(f"Error processing fallback screenshot: {e}")
                self.window._show_notification(_("Failed to process screenshot"))
            finally:
                self._error_callback = None
            return False

        # If Niri screenshot UI closed without writing the file -> cancelled
        if self._niri_finished:
            logger.info("Niri screenshot UI closed without writing target file (cancelled)")
            self._fallback_timeout_id = None
            cleanup_niri_stream()
            self.window.show()
            self.window._show_notification(_("Screenshot cancelled"))
            if self._error_callback:
                self._error_callback("Screenshot cancelled")
            self._error_callback = None
            return False

        # For non-Niri CLI tools: if process exited without creating target file
        if proc and proc.poll() is not None and not self._is_niri_cmd(proc):
            logger.info(f"Fallback process exited with code {proc.returncode} without target file")
            self._fallback_timeout_id = None
            cleanup_niri_stream()
            self.window.show()
            self.window._show_notification(_("Screenshot cancelled"))
            if self._error_callback:
                self._error_callback("Screenshot cancelled")
            self._error_callback = None
            return False

        # Timeout after 30 seconds if file is not created
        if time.time() - self._fallback_start_time > 30:
            logger.warning("Fallback screenshot timed out")
            self._fallback_timeout_id = None
            cleanup_niri_stream()
            self.window.show()
            self.window._show_notification(_("Failed to take screenshot"))
            if self._error_callback:
                self._error_callback("Timed out waiting for screenshot")
            self._error_callback = None
            if proc and proc.poll() is None:
                proc.kill()
            return False

        return True

    def _is_niri_cmd(self, proc: subprocess.Popen) -> bool:
        if hasattr(proc, 'args'):
            args = proc.args
            if isinstance(args, list):
                return any('niri' in str(a) for a in args)
            elif isinstance(args, str):
                return 'niri' in args
        return False

    def _handle_screenshot_uri(self, uri: str) -> None:
        try:
            file = Gio.File.new_for_uri(uri)
            original_path = file.get_path()
            success, contents, _unused = file.load_contents(None)
            if not success or not contents:
                raise Exception("Failed to load screenshot data")

            filename = TimestampedFilenameGenerator().generate(_("Edited Screenshot From %Y-%m-%d %H-%M-%S")) + ".png"
            temp_path = os.path.join(self.temp_dir, filename)

            with open(temp_path, 'wb') as f:
                f.write(contents)

            self._set_image_and_update_ui(temp_path, ImageOrigin.Screenshot, screenshot_path=original_path, copy_after_processing=True)
            self.window._show_notification(_("Screenshot captured!"))

            if self._success_callback:
                self._success_callback()

        except Exception as e:
            logger.error(f"Error processing screenshot: {e}")
            self.window._show_notification(_("Failed to process screenshot"))

    def load_path_as_screenshot(self, file_path: str) -> None:
        try:
            file = Gio.File.new_for_path(file_path)
            uri = file.get_uri()
            self._screenshot_uris.append(uri)
            self._update_delete_action_state()

            filename = TimestampedFilenameGenerator().generate(_("Edited Screenshot From %Y-%m-%d %H-%M-%S")) + ".png"
            new_path = os.path.join(self.temp_dir, filename)

            shutil.copy(file_path, new_path)

            self._set_image_and_update_ui(file_path, ImageOrigin.FakeScreenshot, screenshot_path=file_path, copy_after_processing=True)

            self.window._show_notification(_("Screenshot captured!"))

        except Exception as e:
            logger.error(f"Error loading screenshot from path: {e}")
            self.window._show_notification(_("Failed to load screenshot"))

    def get_screenshot_uris(self) -> list[str]:
        return self._screenshot_uris.copy()

    def delete_screenshots(self) -> None:
        for uri in self._screenshot_uris:
            try:
                file = Gio.File.new_for_uri(uri)
                file.trash(None)
            except Exception as e:
                logger.error(f"Failed to trash screenshot {uri}: {e}")

        self._screenshot_uris.clear()
        self._update_delete_action_state()


class CommandlineLoader(BaseImageLoader):
    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str) -> None:
        super().__init__(window, temp_dir)

    def load_from_file(self, file_path: str) -> None:
        try:
            if not file_path:
                logger.info("No file path provided")
                return

            if not os.path.isfile(file_path):
                logger.info(f"File does not exist: {file_path}")
                return

            if not self._is_supported_format(file_path):
                logger.info(f"Unsupported file format: {file_path}")
                return

            self._set_image_and_update_ui(file_path, ImageOrigin.CommandLine)

        except Exception as e:
            logger.error(f"Error loading file from command line: {e}")

class SourceImageLoader(BaseImageLoader):
    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str) -> None:
        super().__init__(window, temp_dir)
        self._generator_window: Optional[SourceImageGeneratorWindow] = None

    def open_generator(self) -> None:
        if self._generator_window and self._generator_window.get_visible():
            self._generator_window.present()
            return

        self._generator_window = SourceImageGeneratorWindow(parent_window=self.window, temp_dir=self.temp_dir, export_callback=self.load_generated_image)
        self._generator_window.set_transient_for(self.window)
        self._generator_window.connect("destroy", self._on_generator_window_destroyed)
        self._generator_window.show()

    def _on_generator_window_destroyed(self, window: Gtk.Window) -> None:
        self._generator_window = None

    def load_generated_image(self, image_path: str) -> None:
        if not image_path or not os.path.isfile(image_path):
            logger.warning(f"Invalid generated image path: {image_path}")
            return

        self._set_image_and_update_ui(image_path, ImageOrigin.SourceImage)
        self.window._show_notification(_("Source snippet Generated!"))

class ImportManager:
    def __init__(self, window: Gtk.ApplicationWindow, temp_dir: str, app: Gtk.Application) -> None:
        self.window: Gtk.ApplicationWindow = window
        self.temp_dir: str = temp_dir

        self.file_loader: FileDialogImageLoader = FileDialogImageLoader(window, temp_dir)
        self.drag_drop_loader: DragDropImageLoader = DragDropImageLoader(window, temp_dir)
        self.clipboard_loader: ClipboardImageLoader = ClipboardImageLoader(window, temp_dir)
        self.screenshot_loader: ScreenshotImageLoader = ScreenshotImageLoader(window, temp_dir, app)
        self.commandline_loader: CommandlineLoader = CommandlineLoader(window, temp_dir)
        self.source_image_loader: SourceImageLoader = SourceImageLoader(window, temp_dir)

    def open_file_dialog(self) -> None:
        self.file_loader.open_file_dialog()

    def _on_drop_action(self, action: Optional[object], param: object) -> None:
        if isinstance(param, GLib.Variant):
            uri = param.get_string()
            file = Gio.File.new_for_uri(uri)
            self.drag_drop_loader.handle_file_drop(None, file, 0, 0)
        else:
            logger.info("ImportManager._on_drop_action: Invalid drop parameter")

    def load_from_clipboard(self) -> None:
        self.clipboard_loader.load_from_clipboard()

    def take_screenshot(
        self,
        flags: Xdp.ScreenshotFlags = Xdp.ScreenshotFlags.INTERACTIVE,
        on_error_or_cancel: Optional[Callable[[str], None]] = None,
        on_success: Optional[Callable[[], None]] = None
    ) -> None:
        self.screenshot_loader.take_screenshot(flags, on_error_or_cancel, on_success)

    def load_as_screenshot(self, file_path: str):
        self.screenshot_loader.load_path_as_screenshot(file_path)

    def get_screenshot_uris(self) -> list[str]:
        return self.screenshot_loader.get_screenshot_uris()

    def delete_screenshots(self) -> None:
        return self.screenshot_loader.delete_screenshots()

    def load_from_file(self, file_path: str) -> None:
        self.commandline_loader.load_from_file(file_path)

    def generate_from_source_code(self) -> None:
        self.source_image_loader.open_generator()
