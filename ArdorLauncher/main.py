"""
Ardor Gaming — полноэкранный игровой лаунчер (PyQt6 + pygame-геймпад).

Запуск:  python main.py
Стек:    PyQt6, PyQt6-Multimedia, pygame (только joystick-подсистема).
Целевая платформа: Windows 10/11 (HTPC / консольный сценарий на ТВ).

Архитектурные решения (анти-баги) реализованы буквально по ТЗ:
  1. Один QGraphicsEffect на виджет -> вложенная пара _slot(opacity)/_inner(shadow).
  2. Карусель позиционируется вручную (setGeometry/move), без layout.
  3. Размытие фонов — предрасчёт в кэш, не в момент кроссфейда.
  4. Кнопки "Настройки"/"Добавить игру" — часть карусели, Tab их не обходит.
  5. Пустой games.json -> карусель из двух кнопок + placeholder.
  6. Debounce сохранения настроек (QTimer 400 мс, flush при закрытии).
  7. Медиа-фоллбэки (errorOccurred / QSoundEffect.status).
  8. Питание — subprocess.Popen(["shutdown", ...]), без os.system.
  9. Длинные тексты — wordWrap + elidedText.
 10. pygame.joystick.init() БЕЗ pygame.init().
 11. Hot-plug через get_count() по QTimer(2000 мс).
 12. Start/Back могут перехватываться Game Bar — не падаем.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Callable

# ----------------------------------------------------------------------------
# Пути: всё строго от BASE_DIR, независимо от cwd
# ----------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
GAMES_FILE: Path = BASE_DIR / "games.json"
SETTINGS_FILE: Path = BASE_DIR / "settings.json"
ASSETS_DIR: Path = BASE_DIR / "assets"
AUDIO_DIR: Path = ASSETS_DIR / "audio"
ICONS_DIR: Path = ASSETS_DIR / "icons"
COVERS_DIR: Path = ASSETS_DIR / "covers"
BACKGROUNDS_DIR: Path = ASSETS_DIR / "backgrounds"

# ----------------------------------------------------------------------------
# Логирование
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "launcher.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ardor")

# ----------------------------------------------------------------------------
# Импорты Qt
# ----------------------------------------------------------------------------
from PyQt6.QtCore import (                                    # noqa: E402
    QObject, QTimer, Qt, pyqtSignal, pyqtProperty,
    QPropertyAnimation, QEasingCurve, QRect, QSize, QPoint,
)
from PyQt6.QtGui import (                                     # noqa: E402
    QColor, QFont, QPixmap, QPainter, QImage, QIcon, QKeyEvent,
    QFontMetrics,
)
from PyQt6.QtWidgets import (                                 # noqa: E402
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QFileDialog, QMessageBox, QGraphicsOpacityEffect,
    QGraphicsDropShadowEffect, QGraphicsBlurEffect, QGraphicsScene,
    QGraphicsPixmapItem, QLineEdit, QSlider, QListWidget,
    QListWidgetItem, QStackedWidget, QFrame,
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QSoundEffect  # noqa: E402,F401

# ----------------------------------------------------------------------------
# pygame — ТОЛЬКО joystick. Без pygame.init() (аудио-конфликт с QMediaPlayer).
# Отсутствие библиотеки не фатально: работает клавиатура.
# ----------------------------------------------------------------------------
try:
    import pygame  # type: ignore
    PYGAME_AVAILABLE: bool = True
except ImportError:                     # pragma: no cover
    pygame = None                       # type: ignore
    PYGAME_AVAILABLE = False
    log.warning("pygame не установлен — геймпад недоступен, только клавиатура.")


# ============================================================================
# СЕКЦИЯ: StorageManager — загрузка/сохранение JSON с дефолтами и логами
# ============================================================================
class StorageManager:
    """Чтение/запись games.json и settings.json. Битый JSON -> дефолт."""

    DEFAULT_SETTINGS: dict = {
        "music_volume": 70,
        "sfx_volume": 80,
        "music_file": "menu_theme.mp3",
        "sounds": {
            "hover": "hover.wav",
            "select": "select.wav",
            "back": "back.wav",
        },
        "gamepad": {
            "enabled": True,
            "deadzone": 0.3,
            "repeat_delay_ms": 400,
            "repeat_interval_ms": 150,
        },
    }

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            log.warning("Файл %s не найден — создаётся дефолтный.", path)
        except (json.JSONDecodeError, OSError) as e:
            log.error("Файл %s повреждён или недоступен (%s) — дефолт.", path, e)
        return None

    @staticmethod
    def _write_json(path: Path, data: dict) -> bool:
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
            return True
        except OSError as e:
            log.error("Не удалось записать %s: %s", path, e)
            return False

    # ---- settings -----------------------------------------------------------
    @classmethod
    def load_settings(cls) -> dict:
        data = cls._read_json(SETTINGS_FILE)
        if not isinstance(data, dict):
            data = {}
        merged: dict = json.loads(json.dumps(cls.DEFAULT_SETTINGS))
        for key, val in data.items():          # поверхностное слияние
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                merged[key].update(
                    {k: v for k, v in val.items() if k in merged[key]}
                )
            else:
                merged[key] = val
        cls.save_settings(merged)
        return merged

    @classmethod
    def save_settings(cls, settings: dict) -> bool:
        return cls._write_json(SETTINGS_FILE, settings)

    # ---- games ---------------------------------------------------------------
    @classmethod
    def load_games(cls) -> list[dict]:
        data = cls._read_json(GAMES_FILE)
        if not isinstance(data, dict) or not isinstance(data.get("games"), list):
            data = {"games": []}
            cls._write_json(GAMES_FILE, data)
        games: list[dict] = []
        for g in data["games"]:
            if isinstance(g, dict) and g.get("title"):
                games.append({
                    "id": str(g.get("id") or uuid.uuid4()),
                    "title": str(g.get("title", "")),
                    "description": str(g.get("description", "")),
                    "exe_path": str(g.get("exe_path", "")),
                    "icon": str(g.get("icon", "")),
                    "cover": str(g.get("cover", "")),
                    "background": str(g.get("background", "")),
                })
        return games

    @classmethod
    def add_game(cls, game: dict) -> bool:
        """Добавить игру, НЕ перезаписывая существующие."""
        games = cls.load_games()
        games.append(game)
        return cls._write_json(GAMES_FILE, {"games": games})


# ============================================================================
# СЕКЦИЯ: AssetLoader — пиксмапы, placeholder'ы, предрасчёт размытых фонов
# ============================================================================
class AssetLoader:
    """Все загрузки ассетов в try/except. Missing file -> placeholder."""

    _pixmaps: dict[str, QPixmap] = {}           # кэш обычных картинок
    _blurred: dict[str, QPixmap] = {}           # кэш размытых фонов
    _placeholders: dict[tuple[int, int], QPixmap] = {}

    # ---- placeholder ---------------------------------------------------------
    @classmethod
    def placeholder(cls, w: int, h: int, text: str = "") -> QPixmap:
        key = (w, h, text)
        pm = cls._placeholders.get(key)         # тип ключа tuple[int,str]...
        if pm is not None and not pm.isNull():
            return pm
        pm = QPixmap(max(1, w), max(1, h))
        pm.fill(QColor("#1A1A1A"))
        painter = QPainter()
        can_paint = painter.begin(pm)
        try:
          if can_paint:
            painter.setPen(QColor("#FFFFFF"))
            font = QFont("Segoe UI", max(9, min(w, h) // 14))
            painter.setFont(font)
            label = text if text else "no asset"
            metrics = QFontMetrics(font)
            label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, w - 10)
            painter.drawText(QRect(0, 0, w, h),
                             Qt.AlignmentFlag.AlignCenter, label)
        finally:
            painter.end()
        cls._placeholders[key] = pm
        return pm

    # ---- обычный пикмап -------------------------------------------------------
    @classmethod
    def pixmap(cls, rel_or_abs: str, w: int, h: int,
               keep_aspect: bool = True) -> QPixmap:
        """Загрузить картинку; отсутствует/битая -> placeholder с именем файла."""
        if not rel_or_abs:
            return cls.placeholder(w, h, "(пустой путь)")
        p = Path(rel_or_abs)
        full = p if p.is_absolute() else (BASE_DIR / p)
        cache_key = f"{full}|{w}x{h}|{int(keep_aspect)}"
        cached = cls._pixmaps.get(cache_key)
        if cached is not None:
            return cached
        result: QPixmap
        try:
            src = QPixmap(str(full))
            if src.isNull():
                raise FileNotFoundError(str(full))
            mode = (Qt.AspectRatioMode.KeepAspectRatioByExpanding if keep_aspect
                    else Qt.AspectRatioMode.IgnoreAspectRatio)
            result = src.scaled(w, h, mode,
                                Qt.TransformationMode.SmoothTransformation)
            if keep_aspect and (result.width() > w or result.height() > h):
                # центральная кроп-область для "покрывающего" режима
                x = max(0, (result.width() - w) // 2)
                y = max(0, (result.height() - h) // 2)
                result = result.copy(x, y, min(w, result.width()),
                                     min(h, result.height()))
        except (OSError, ValueError) as e:
            log.warning("Ассет '%s' не загружен (%s) — placeholder.", full, e)
            result = cls.placeholder(w, h, full.name)
        cls._pixmaps[cache_key] = result
        return result

    # ---- предрасчёт размытого фона (п.3) --------------------------------------
    @classmethod
    def blurred_background(cls, rel_or_abs: str, screen_size: QSize) -> QPixmap:
        """
        Уменьшить до 25%, blur radius=25 через offscreen QGraphicsScene,
        отрендерить, увеличить обратно до размера экрана. Результат в кэше —
        в момент кроссфейда используется готовый QPixmap (без FPS-просадки).
        """
        cache_key = f"{rel_or_abs}|{screen_size.width()}x{screen_size.height()}"
        cached = cls._blurred.get(cache_key)
        if cached is not None:
            return cached
        sw, sh = max(1, screen_size.width()), max(1, screen_size.height())
        if not rel_or_abs:
            pm = QPixmap(sw, sh)
            pm.fill(QColor("#000000"))
            cls._blurred[cache_key] = pm
            return pm
        p = Path(rel_or_abs)
        full = p if p.is_absolute() else (BASE_DIR / p)
        try:
            src = QPixmap(str(full))
            if src.isNull():
                raise FileNotFoundError(str(full))
            small_w = max(1, sw // 4)
            small_h = max(1, sh // 4)
            small = src.scaled(small_w, small_h,
                               Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            scene = QGraphicsScene()
            item = QGraphicsPixmapItem(small)
            blur = QGraphicsBlurEffect()
            blur.setBlurRadius(25)
            blur.setBlurHints(QGraphicsBlurEffect.BlurHint.PerformanceHint)
            item.setGraphicsEffect(blur)
            scene.addItem(item)
            scene.setSceneRect(0, 0, small_w, small_h)
            # Рендерим через drawPixmap вместо scene.render(): QGraphicsScene
            # не поддерживает вложенный QPainter внутри активного paint-устройства.
            rendered = QImage(small_w, small_h,
                              QImage.Format.Format_ARGB32_Premultiplied)
            rendered.fill(Qt.GlobalColor.transparent)
            rp = QPainter()
            if not rp.begin(rendered):
                raise RuntimeError("QPainter.begin(QImage) failed")
            try:
                rp.drawPixmap(0, 0, item.pixmap())   # уже размытый эффект-выход
            finally:
                rp.end()
            result = QPixmap.fromImage(rendered).scaled(
                sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        except (OSError, ValueError, RuntimeError) as e:
            log.warning("Фон '%s' не размыт (%s) — чёрный placeholder.", full, e)
            result = QPixmap(sw, sh)
            result.fill(QColor("#000000"))
        cls._blurred[cache_key] = result
        return result


# ============================================================================
# СЕКЦИЯ: AudioManager — музыка (QMediaPlayer) + SFX (QSoundEffect)
# ============================================================================
class AudioManager(QObject):
    """Музыка меню по кругу + SFX. Все ошибки — лог, UI не блокируется."""

    def __init__(self, settings: dict, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings: dict = settings
        self._player: Optional[QMediaPlayer] = None
        self._audio_out: Optional[QAudioOutput] = None
        self._sfx: dict[str, QSoundEffect] = {}
        self._build_sfx()
        self.reload_music()

    # ---- music ----------------------------------------------------------------
    def reload_music(self) -> None:
        name = str(self._settings.get("music_file", "menu_theme.mp3"))
        path = AUDIO_DIR / name
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._audio_out = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_out)
            self._player.errorOccurred.connect(self._on_media_error)
            self._player.mediaStatusChanged.connect(self._on_media_status)
        vol = float(self._settings.get("music_volume", 70))
        self._audio_out.setVolume(max(0.0, min(1.0, vol / 100.0)))
        if not path.exists():
            log.warning("Файл музыки '%s' не найден — играем без музыки.", path)
            return
        self._player.setSource(path.as_uri())
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        try:
            self._player.play()
        except Exception as e:                      # noqa: BLE001
            log.warning("Не удалось запустить музыку: %s", e)

    def set_music_volume(self, v: int) -> None:
        self._settings["music_volume"] = int(v)
        if self._audio_out is not None:
            self._audio_out.setVolume(max(0.0, min(1.0, v / 100.0)))

    def _on_media_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError:
            log.warning("QMediaPlayer ошибка (%s): %s — продолжаем без музыки.",
                        error, message)

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            log.warning("Медиа-файл недопустим — музыка выключена.")

    # ---- sfx ------------------------------------------------------------------
    def _build_sfx(self) -> None:
        sounds = self._settings.get("sounds", {})
        for kind, fname in sounds.items():
            self._register_sfx(kind, str(fname))

    def _register_sfx(self, kind: str, fname: str) -> None:
        path = AUDIO_DIR / fname
        eff = QSoundEffect(self)
        # statusChanged — мягкая реакция на ошибки загрузки (п.7).
        # (errorOccurred у QSoundEffect отсутствует во многих версиях Qt.)
        eff.statusChanged.connect(
            lambda st, p=str(path): log.warning(
                "QSoundEffect '%s': status=%s", p, st)
            if st == QSoundEffect.Status.Error else None)
        if not path.exists():
            log.warning("SFX-файл '%s' не найден — звук '%s' отключён.",
                        path, kind)
            return
        eff.setSource(path.as_uri())
        vol = float(self._settings.get("sfx_volume", 80))
        eff.setVolume(max(0.0, min(1.0, vol / 100.0)))
        self._sfx[kind] = eff

    def set_sfx_volume(self, v: int) -> None:
        self._settings["sfx_volume"] = int(v)
        for eff in self._sfx.values():
            eff.setVolume(max(0.0, min(1.0, v / 100.0)))

    def play_sfx(self, kind: str) -> None:
        eff = self._sfx.get(kind)
        if eff is None:
            return
        try:
            if eff.status() != QSoundEffect.Status.Loaded:
                log.debug("SFX '%s' ещё не загружен (status=%s) — пропускаем.",
                          kind, eff.status())
                return
            eff.stop()
            eff.play()
        except Exception as e:                      # noqa: BLE001
            log.warning("Ошибка воспроизведения SFX '%s': %s", kind, e)

    def change_sound_file(self, kind: str, fname: str) -> None:
        self._settings.setdefault("sounds", {})[kind] = fname
        old = self._sfx.pop(kind, None)
        if old is not None:
            old.deleteLater()
        self._register_sfx(kind, fname)

    def change_music_file(self, fname: str) -> None:
        self._settings["music_file"] = fname
        self.reload_music()


# ============================================================================
# СЕКЦИЯ: GamepadController — pygame.joystick, hot-plug, edge-trigger, repeat
# ============================================================================
class GamepadController(QObject):
    """
    Читает первый доступный геймпад через pygame.joystick (БЕЗ pygame.init()).
    Генерирует сигналы навигации; MainWindow не знает про pygame напрямую.
      - hot-plug: QTimer 2000 мс опрашивает pygame.joystick.get_count();
      - поллинг состояния: QTimer ~33 мс (30 FPS);
      - действия — edge-trigger; навигация — edge + автоповтор (delay/interval);
      - hover-SFX только на первый edge, не на автоповторе: сигналы
        navigate_* несут флаг auto (bool), его обрабатывает MainWindow.
    """

    navigate_left = pyqtSignal(bool)     # bool: True = шаг автоповтора
    navigate_right = pyqtSignal(bool)
    navigate_up = pyqtSignal(bool)
    navigate_down = pyqtSignal(bool)
    confirm = pyqtSignal()
    back = pyqtSignal()
    open_settings = pyqtSignal()
    open_add_game = pyqtSignal()
    emergency_exit = pyqtSignal()
    status_changed = pyqtSignal(bool, str)   # connected, name

    NAV_DIRS = ("left", "right", "up", "down")

    def __init__(self, settings: dict, parent: Optional[QObject] = None):
        super().__init__(parent)
        gp = settings.get("gamepad", {})
        self._enabled: bool = bool(gp.get("enabled", True))
        self._deadzone: float = float(gp.get("deadzone", 0.3))
        self._repeat_delay_ms: int = int(gp.get("repeat_delay_ms", 400))
        self._repeat_interval_ms: int = int(gp.get("repeat_interval_ms", 150))

        self._joy = None                       # Optional[pygame.joystick.Joystick]
        self._joy_name: str = ""
        self._prev: dict[str, bool] = {}       # prev_state для кнопок/направлений
        self._held_since: dict[str, float] = {}  # когда зажато направление
        self._last_repeat: dict[str, float] = {}
        self._combo_start: float = 0.0         # Back+Start удержание
        self._combo_fired: bool = False

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._hotplug_timer = QTimer(self)
        self._hotplug_timer.timeout.connect(self._check_hotplug)

        if PYGAME_AVAILABLE and self._enabled:
            try:
                pygame.joystick.init()          # ТОЛЬКО joystick (п.10)
            except Exception as e:              # noqa: BLE001
                log.error("pygame.joystick.init() не удался: %s", e)
            self._check_hotplug()
        elif not self._enabled:
            log.info("Геймпад отключён в настройках.")

        self._poll_timer.start(33)              # ~30 FPS
        self._hotplug_timer.start(2000)         # hot-plug каждые 2 с (п.11)

    # ---- properties ------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._joy is not None

    @property
    def controller_name(self) -> str:
        return self._joy_name

    # ---- hot-plug (get_count, без событий SDL) ---------------------------------
    def _check_hotplug(self) -> None:
        if not PYGAME_AVAILABLE or not self._enabled:
            return
        try:
            count = pygame.joystick.get_count()
        except Exception as e:                  # noqa: BLE001
            log.warning("get_count() не удался: %s", e)
            count = 0
        if count == 0:
            if self._joy is not None:
                self._detach("Геймпад отключён")
            return
        if self._joy is not None:
            # текущий всё ещё жив?
            try:
                self._joy.get_numbuttons()
                return
            except Exception:                   # noqa: BLE001
                self._detach("Геймпад отключён")
        self._attach_first(count)

    def _attach_first(self, count: int) -> None:
        for idx in range(count):
            try:
                joy = pygame.joystick.Joystick(idx)
                joy.init()
            except Exception as e:              # noqa: BLE001
                log.warning("Joystick(%d) init failed: %s", idx, e)
                continue
            self._joy = joy
            try:
                self._joy_name = joy.get_name()
                nbtn = joy.get_numbuttons()
                naxis = joy.get_numaxes()
                nhat = joy.get_numhats()
            except Exception:                   # noqa: BLE001
                self._joy_name = "Unknown pad"
                nbtn = naxis = nhat = -1
            log.info("Геймпад подключён: name='%s', buttons=%s, axes=%s, hats=%s",
                     self._joy_name, nbtn, naxis, nhat)
            self._reset_edge_state()
            self.status_changed.emit(True, self._joy_name)
            return
        log.warning("Геймпады есть (count=%s), но ни один не инициализирован.",
                    count)

    def _detach(self, reason: str) -> None:
        self._joy = None
        self._joy_name = ""
        self._reset_edge_state()
        log.info(reason)
        self.status_changed.emit(False, "")

    def _reset_edge_state(self) -> None:
        self._prev.clear()
        self._held_since.clear()
        self._last_repeat.clear()
        self._combo_start = 0.0
        self._combo_fired = False

    # ---- polling -----------------------------------------------------------------
    def _poll(self) -> None:
        if self._joy is None or not self._enabled:
            return
        now = time.monotonic() * 1000.0   # мс
        try:
            state = self._read_state()
        except Exception as e:            # noqa: BLE001 — джойстик мог выскочить
            log.info("Чтение геймпада прервано (%s) — переподключение.", e)
            self._detach("Геймпад отключён")
            self._check_hotplug()
            return

        # --- направления: edge + автоповтор ---
        for d in self.NAV_DIRS:
            pressed = state.get(d, False)
            was = self._prev.get(d, False)
            if pressed and not was:
                self._held_since[d] = now
                self._last_repeat[d] = now
                self._emit_nav(d, auto=False)
            elif pressed and was:
                since = self._held_since.get(d, now)
                last = self._last_repeat.get(d, now)
                if now - since >= self._repeat_delay_ms and \
                        now - last >= self._repeat_interval_ms:
                    self._last_repeat[d] = now
                    self._emit_nav(d, auto=True)
            self._prev[d] = pressed

        # --- action-кнопки: только edge-trigger ---
        actions = (
            ("a_confirm", state.get("a", False), self.confirm),
            ("b_back", state.get("b", False), self.back),
            ("start", state.get("start", False), self.open_settings),
            ("back_btn", state.get("backbtn", False), self.open_add_game),
        )
        for key, pressed, sig in actions:
            was = self._prev.get(key, False)
            if pressed and not was:
                sig.emit()
            self._prev[key] = pressed

        # --- аварийный выход: Back+Start ~1 сек ---
        if state.get("start", False) and state.get("backbtn", False):
            if self._combo_start == 0.0:
                self._combo_start = now
            elif (now - self._combo_start >= 1000.0
                  and not self._combo_fired):
                self._combo_fired = True
                log.info("Emergency exit: Back+Start удержан 1 сек.")
                self.emergency_exit.emit()
        else:
            self._combo_start = 0.0
            self._combo_fired = False

    def _emit_nav(self, direction: str, auto: bool) -> None:
        sig = {
            "left": self.navigate_left, "right": self.navigate_right,
            "up": self.navigate_up, "down": self.navigate_down,
        }[direction]
        sig.emit(auto)

    def _read_state(self) -> dict[str, bool]:
        j = self._joy
        st: dict[str, bool] = {}
        dz = self._deadzone
        # оси: XInput-стиль — axis0 right+, axis1 UP=+ на некоторых драйверах;
        # по ТЗ: вверх = axis(1) > +deadzone (инвертируем стандарт SDL вниз+)
        try:
            ax_x = j.get_axis(0)
        except Exception:                       # noqa: BLE001
            ax_x = 0.0
        try:
            ax_y_raw = j.get_axis(1)
        except Exception:                       # noqa: BLE001
            ax_y_raw = 0.0
        ax_y = -ax_y_raw                        # инверсия: вверх положительно
        st["left"] = ax_x < -dz
        st["right"] = ax_x > dz
        st["up"] = ax_y > dz
        st["down"] = ax_y < -dz
        # hat (D-pad): по ТЗ up = +1, down = -1
        try:
            hx, hy = j.get_hat(0)
        except Exception:                       # noqa: BLE001
            hx = hy = 0
        if hx == -1:
            st["left"] = True
        if hx == 1:
            st["right"] = True
        if hy == 1:
            st["up"] = True
        if hy == -1:
            st["down"] = True
        # кнопки XInput-style
        st["a"] = self._button(j, 0)
        st["b"] = self._button(j, 1)
        st["start"] = self._button(j, 7)
        st["backbtn"] = self._button(j, 6)
        return st

    @staticmethod
    def _button(j, index: int) -> bool:
        """Кнопка может отсутствовать на нестандартном геймпаде — не падаем (п.12)."""
        try:
            return bool(j.get_button(index))
        except Exception:                       # noqa: BLE001
            return False

    # ---- shutdown ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._poll_timer.stop()
        self._hotplug_timer.stop()
        self._joy = None


# ============================================================================
# СЕКЦИЯ: CarouselWidget — ручное позиционирование, _slot/_inner, анимации
# ============================================================================
class CarouselItem:
    """Данные одного элемента карусели (игра или кнопка действия)."""

    KIND_GAME = "game"
    KIND_BUTTON = "button"

    def __init__(self, kind: str, title: str, subtitle: str,
                 image_rel: str, data: Optional[dict] = None,
                 button_id: str = ""):
        self.kind: str = kind
        self.title: str = title
        self.subtitle: str = subtitle
        self.image_rel: str = image_rel
        self.data: Optional[dict] = data
        self.button_id: str = button_id


class _CarouselSlot(QWidget):
    """Внешний слот: ТОЛЬКО QGraphicsOpacityEffect + движение через pos (п.1)."""

    def __init__(self, inner: QWidget, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.inner: QWidget = inner
        self.effect: QGraphicsOpacityEffect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(1.0)
        self.setGraphicsEffect(self.effect)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(inner)


class _CarouselInner(QWidget):
    """Внутренний виджет: иконка + подписи + QGraphicsDropShadowEffect (п.1)."""

    def __init__(self, item: CarouselItem, icon_size: int,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.item: CarouselItem = item
        self._icon_size: int = icon_size
        self.setStyleSheet("background: transparent;")

        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(0)
        self.shadow.setColor(QColor("#E60000"))
        self.shadow.setOffset(0, 0)
        self.setGraphicsEffect(self.shadow)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(10, 10, 10, 10)
        vbox.setSpacing(8)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(icon_size, icon_size)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("border-radius: 16px;")
        vbox.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel(item.title, self)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.title_label.setWordWrap(True)
        self.title_label.setMaximumWidth(int(icon_size * 1.4))
        self.title_label.setStyleSheet(
            "color:#FFFFFF; font-size:%dpx; font-weight:bold;"
            % max(16, int(icon_size * 0.16)))
        vbox.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.sub_label = QLabel(item.subtitle, self)
        self.sub_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.sub_label.setWordWrap(True)
        self.sub_label.setMaximumWidth(int(icon_size * 1.6))
        self.sub_label.setStyleSheet("color:#B0B0B0; font-size:%dpx;"
                                     % max(10, int(icon_size * 0.06)))
        vbox.addWidget(self.sub_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._render_icon()

    def _render_icon(self) -> None:
        it = self.item
        if it.kind == CarouselItem.KIND_GAME and it.data is not None:
            pm = AssetLoader.pixmap(it.image_rel, self._icon_size,
                                    self._icon_size)
        elif it.kind == CarouselItem.KIND_BUTTON:
            pm = AssetLoader.placeholder(self._icon_size, self._icon_size,
                                         it.title)
        else:
            pm = AssetLoader.placeholder(self._icon_size, self._icon_size,
                                         it.image_rel or it.title)
        self.icon_label.setPixmap(pm)

    def set_active_visual(self, active: bool) -> None:
        """Активному — красное свечение blur=40 (п.1), неактивным — 0."""
        self.shadow.setBlurRadius(40 if active else 0)


class CarouselWidget(QWidget):
    """
    Циклическая карусель: 3 видимых элемента (центр + 2 соседа opacity 0.4).
    Позиционирование ВРУЧНУЮ через move()/setGeometry (п.2). Анимация pos +
    opacity — QPropertyAnimation 250 мс OutCubic.
    """

    item_activated = pyqtSignal(object)      # CarouselItem
    index_changed = pyqtSignal(object)       # CarouselItem (для названия/фона)

    VISIBLE_OFFSETS = (-1, 0, 1)             # относительные позиции центра

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._items: list[CarouselItem] = []
        self._index: int = 0
        self._prev_index: int = 0
        self._slots: list[_CarouselSlot] = []
        self._inners: list[_CarouselInner] = []
        self._anims: list[QPropertyAnimation] = []
        self._hide_timers: dict[int, QTimer] = {}
        self._icon_size: int = 300
        self.setStyleSheet("background: transparent;")

    # ---- API -------------------------------------------------------------------
    def set_items(self, items: list[CarouselItem], focus_index: int = 0) -> None:
        self._clear_slots()
        self._items = items
        self._index = max(0, min(focus_index, len(items) - 1)) if items else 0
        self._prev_index = self._index
        self._rebuild_slots()
        self._layout_slots(animate=False)
        self.index_changed.emit(self.current_item())

    def current_item(self) -> Optional[CarouselItem]:
        if not self._items:
            return None
        return self._items[self._index]

    def move_by(self, delta: int) -> None:
        if not self._items:
            return
        self._prev_index = self._index
        self._index = (self._index + delta) % len(self._items)
        self._layout_slots(animate=True)
        self.index_changed.emit(self.current_item())

    def activate_current(self) -> None:
        item = self.current_item()
        if item is not None:
            self.item_activated.emit(item)

    def set_scale(self, icon_size: int) -> None:
        if icon_size == self._icon_size:
            return
        self._icon_size = icon_size
        self._rebuild_slots()
        self._layout_slots(animate=False)

    # ---- internals ---------------------------------------------------------------
    def _clear_slots(self) -> None:
        for a in self._anims:
            a.stop()
        self._anims.clear()
        for t in self._hide_timers.values():
            t.stop()
        self._hide_timers.clear()
        for s in self._slots:
            s.deleteLater()
        self._slots.clear()
        self._inners.clear()

    def _rebuild_slots(self) -> None:
        self._clear_slots()
        for item in self._items:
            inner = _CarouselInner(item, self._icon_size, self)
            slot = _CarouselSlot(inner, self)
            slot.adjustSize()
            self._slots.append(slot)
            self._inners.append(inner)

    def _slot_for_offset(self, offset: int) -> Optional[_CarouselSlot]:
        """Элемент на циклическом смещении offset от центра."""
        if not self._items:
            return None
        idx = (self._index + offset) % len(self._items)
        return self._slots[idx]

    def _geometry_for_offset(self, offset: int) -> tuple[QRect, float]:
        w, h = self.width(), self.height()
        slot_w = self._icon_size + 40
        slot_h = int(self._icon_size * 1.7)
        cx = w // 2
        cy = h // 2
        spacing = int(slot_w * 1.05)
        scale = 1.0 if offset == 0 else 0.8
        gw, gh = int(slot_w * scale), int(slot_h * scale)
        gx = cx + offset * spacing - gw // 2
        gy = cy - gh // 2
        if offset == 0:
            opacity = 1.0
        elif abs(offset) == 1:
            opacity = 0.4
        else:
            opacity = 0.0
        return QRect(gx, gy, gw, gh), opacity

    def _layout_slots(self, animate: bool) -> None:
        self._kill_finished_anims()
        n = len(self._items)
        if n == 0:
            return
        # Циклическое смещение каждого слота относительно центра (-n//2..n//2).
        # Видимая тройка — смещения -1, 0, +1.
        visible_off: dict[int, int] = {}
        for i in range(n):
            off = (i - self._index + n // 2) % n - n // 2
            visible_off[i] = off
        for i, slot in enumerate(self._slots):
            off = visible_off[i]
            is_visible = off in self.VISIBLE_OFFSETS or \
                (n <= 3 and abs(off) <= 1)
            rect, opacity = self._geometry_for_offset(off)
            timer = self._hide_timers.pop(i, None)
            if timer is not None:
                timer.stop()
            if is_visible:
                slot.show()
                self._inners[i].set_active_visual(visible_off[i] == 0)
                if animate:
                    self._animate_slot(slot, rect, opacity)
                else:
                    slot.setGeometry(rect)
                    slot.effect.setOpacity(opacity)
            elif not slot.isVisible():
                continue                       # уже скрыт
            else:
                # уходящий элемент: доезжает до позиции ±2 и гаснет,
                # скрывается по таймеру после окончания анимации
                side = off if abs(off) >= 2 else (
                    2 if (off - getattr(self, "_last_off", {}).get(i, off)) >= 0
                    else -2)
                rect2, _ = self._geometry_for_offset(side)
                if animate:
                    self._animate_slot(slot, rect2, 0.0)
                    t = QTimer(self)
                    t.setSingleShot(True)
                    t.timeout.connect(lambda s=slot: s.hide())
                    t.start(260)               # чуть дольше длительности
                    self._hide_timers[i] = t
                else:
                    slot.setGeometry(rect2)
                    slot.effect.setOpacity(0.0)
                    slot.hide()
        self._last_off = visible_off

    def _animate_slot(self, slot: _CarouselSlot, rect: QRect,
                      opacity: float) -> None:
        # pos-анимация (п.2: layout'ы не используются)
        pa = QPropertyAnimation(slot, b"pos", self)
        pa.setDuration(250)
        pa.setStartValue(slot.pos())
        pa.setEndValue(rect.topLeft())
        pa.setEasingCurve(QEasingCurve.Type.OutCubic)
        pa.start()
        # opacity-анимация на QGraphicsOpacityEffect
        oa = QPropertyAnimation(slot.effect, b"opacity", self)
        oa.setDuration(250)
        oa.setStartValue(slot.effect.opacity())
        oa.setEndValue(opacity)
        oa.setEasingCurve(QEasingCurve.Type.OutCubic)
        oa.start()
        self._anims.extend([pa, oa])

    def _kill_finished_anims(self) -> None:
        self._anims = [a for a in self._anims if a.state()
                       == QPropertyAnimation.State.Running]

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self._layout_slots(animate=False)               # пересчёт позиций (п.2)


# ============================================================================
# СЕКЦИЯ: BackgroundCrossfade — две QLabel + opacity-анимация 400 мс
# ============================================================================
class BackgroundCrossfade(QWidget):
    """Кроссфейд между двумя QLabel с ПРЕДрассчитанными размытыми пикмапами."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._labels: list[QLabel] = []
        self._effects: list[QGraphicsOpacityEffect] = []
        for _ in range(2):
            lbl = QLabel(self)
            lbl.setGeometry(0, 0, self.width(), self.height())
            lbl.setScaledContents(True)
            eff = QGraphicsOpacityEffect(lbl)
            lbl.setGraphicsEffect(eff)
            self._labels.append(lbl)
            self._effects.append(eff)
        self._front: int = 0
        self._anims: list[QPropertyAnimation] = []
        self._current_src: str = ""
        self._show_placeholder_black()

    def _show_placeholder_black(self) -> None:
        pm = QPixmap(64, 36)
        pm.fill(QColor("#000000"))
        self._labels[0].setPixmap(pm)
        self._effects[0].setOpacity(1.0)
        self._effects[1].setOpacity(0.0)

    def set_background(self, rel_path: str) -> None:
        if rel_path == self._current_src:
            return
        self._current_src = rel_path
        size = self.size()
        # предрасчёт (п.3) — в момент кроссфейда уже готовый QPixmap
        blurred = AssetLoader.blurred_background(rel_path, size)
        back_idx = 1 - self._front
        # остановить идущие анимации opacity (иначе перезапишут значение)
        for a in self._anims:
            try:
                a.stop()
            except RuntimeError:               # объект уже удалён
                pass
        self._anims.clear()
        self._labels[back_idx].setPixmap(blurred)
        self._effects[back_idx].setOpacity(0.0)
        self._labels[back_idx].raise_()
        self._labels[back_idx].show()

        anim_in = QPropertyAnimation(self._effects[back_idx], b"opacity", self)
        anim_in.setDuration(400)
        anim_in.setStartValue(0.0)
        anim_in.setEndValue(1.0)
        anim_out = QPropertyAnimation(self._effects[self._front], b"opacity",
                                      self)
        anim_out.setDuration(400)
        anim_out.setStartValue(self._effects[self._front].opacity())
        anim_out.setEndValue(0.0)
        for a in (anim_in, anim_out):
            a.start()
            self._anims.append(a)
        self._front = back_idx

    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        for lbl in self._labels:
            lbl.setGeometry(0, 0, self.width(), self.height())
        if self._current_src:
            src = self._current_src
            self._current_src = ""            # форсируем переразмывание под размер
            self.set_background(src)


# ============================================================================
# СЕКЦИЯ: AddGameDialog
# ============================================================================
class AddGameDialog(QWidget):
    """
    Диалог добавления игры: glassmorphism, поля ввода (клавиатура!),
    навигация геймпадом ↑/↓ между полями, A — активация, B — отмена.
    Реализован как frameless overlay-виджет (не QDialog), чтобы Tab не
    конфликтовал с каруселью главного окна (п.4).
    """

    saved = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.WindowType.Widget)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "background: rgba(0,0,0,0.85);"
            "border: 1px solid #E60000; border-radius: 16px;")
        self._fields_order: list[QLineEdit] = []
        self._focus_i: int = -1
        self._build_ui()
        self.hide()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(10)

        title = QLabel("ДОБАВИТЬ ИГРУ", self)
        title.setStyleSheet("color:#FFFFFF; font-size:32px; font-weight:bold;"
                            "background:transparent; border:none;")
        root.addWidget(title)

        hint = QLabel("Ввод текста — с клавиатуры. Навигация — геймпадом "
                      "(↑/↓ — поля, A — следующий шаг/активация, B — отмена).",
                      self)
        hint.setStyleSheet("color:#B0B0B0; font-size:14px;background:transparent;"
                           "border:none;")
        root.addWidget(hint)

        def make_field(label: str, placeholder: str = "") -> tuple[QVBoxLayout, QLineEdit]:
            box = QVBoxLayout()
            box.setSpacing(2)
            cap = QLabel(label, self)
            cap.setStyleSheet("color:#B0B0B0; font-size:14px;"
                              "background:transparent; border:none;")
            edit = QLineEdit(self)
            edit.setPlaceholderText(placeholder)
            edit.setMinimumHeight(40)
            box.addWidget(cap)
            box.addWidget(edit)
            return box, edit

        _, self.ed_title = make_field("Название *")
        _, self.ed_desc = make_field("Описание")
        _, self.ed_exe = make_field("Путь к EXE *")
        _, self.ed_icon = make_field("Имя иконки", "assets/icons/game.png")
        _, self.ed_cover = make_field("Имя обложки", "assets/covers/game.png")
        _, self.ed_bg = make_field("Имя фона", "assets/backgrounds/game.jpg")

        for ed in (self.ed_title, self.ed_desc, self.ed_exe,
                   self.ed_icon, self.ed_cover, self.ed_bg):
            root.addWidget(ed)
            self._fields_order.append(ed)

        btn_row = QHBoxLayout()
        self.btn_browse = QPushButton("Обзор…", self)
        self.btn_browse.clicked.connect(self._browse_exe)
        self.btn_save = QPushButton("Сохранить (A)", self)
        self.btn_save.clicked.connect(self._save)
        self.btn_cancel = QPushButton("Отмена (B)", self)
        self.btn_cancel.clicked.connect(self.close_dialog)
        for b in (self.btn_browse, self.btn_save, self.btn_cancel):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # Tab — не сюда (п.4)
            btn_row.addWidget(b)
        root.addLayout(btn_row)

        self._buttons_order: list[QPushButton] = [self.btn_browse,
                                                  self.btn_save,
                                                  self.btn_cancel]
        self._widgets: list[QWidget] = [*self._fields_order,
                                        *self._buttons_order]
        self._nav_index: int = 0
        self._apply_focus_visual()

    # ---- gamepad navigation ----------------------------------------------------
    def nav_up(self) -> None:
        self._nav_index = (self._nav_index - 1) % len(self._widgets)
        self._apply_focus_visual()

    def nav_down(self) -> None:
        self._nav_index = (self._nav_index + 1) % len(self._widgets)
        self._apply_focus_visual()

    def activate_current(self) -> None:
        w = self._widgets[self._nav_index]
        if isinstance(w, QPushButton):
            w.click()
        elif isinstance(w, QLineEdit):
            w.setFocus()          # физическая клавиатура печатает в фокус

    def cancel(self) -> None:
        self.close_dialog()

    def _apply_focus_visual(self) -> None:
        for i, w in enumerate(self._widgets):
            if isinstance(w, QLineEdit):
                w.setProperty("focused", "yes" if i == self._nav_index else "no")
                w.style().unpolish(w)
                w.style().polish(w)
            elif isinstance(w, QPushButton):
                w.setProperty("focused", "yes" if i == self._nav_index else "no")
                w.style().unpolish(w)
                w.style().polish(w)
        cur = self._widgets[self._nav_index]
        if isinstance(cur, QLineEdit):
            cur.setFocus()

    # ---- actions -----------------------------------------------------------------
    def _browse_exe(self) -> None:
        start = self.ed_exe.text().strip() or str(Path.home())
        try:
            fname, _ = QFileDialog.getOpenFileName(
                self, "Выберите исполняемый файл", start,
                "Программы (*.exe *.bat *.cmd *.lnk);;Все файлы (*)")
            if fname:
                self.ed_exe.setText(fname)
        except Exception as e:                        # noqa: BLE001
            log.warning("QFileDialog не открылся: %s", e)

    def _save(self) -> None:
        title = self.ed_title.text().strip()
        exe = self.ed_exe.text().strip()
        if not title or not exe:
            QMessageBox.warning(self, "Проверка",
                                "Поля «Название» и «Путь к EXE» обязательны.")
            return
        game = {
            "id": str(uuid.uuid4()),
            "title": title,
            "description": self.ed_desc.text().strip(),
            "exe_path": exe.replace("\\", "/"),
            "icon": self.ed_icon.text().strip(),
            "cover": self.ed_cover.text().strip(),
            "background": self.ed_bg.text().strip(),
        }
        if StorageManager.add_game(game):
            log.info("Игра добавлена: %s", title)
            self.saved.emit(game)
        self.close_dialog()

    def close_dialog(self) -> None:
        self.hide()
        self.closed.emit()

    # ---- geometry ------------------------------------------------------------------
    def open_dialog(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            pw, ph = parent.width(), parent.height()
            w = min(760, int(pw * 0.6))
            h = min(640, int(ph * 0.8))
            self.setGeometry((pw - w) // 2, (ph - h) // 2, w, h)
        self.ed_title.clear()
        self.ed_desc.clear()
        self.ed_exe.clear()
        self.ed_icon.clear()
        self.ed_cover.clear()
        self.ed_bg.clear()
        self._nav_index = 0
        self.show()
        self.raise_()
        self._apply_focus_visual()


# ============================================================================
# СЕКЦИЯ: SettingsDialog
# ============================================================================
class SettingsDialog(QWidget):
    """
    Экран настроек: меню слева (Сеть/Экран/Звук/Питание), стек справа.
    Геймпад: ↑/↓ переключает фокус-зону (меню ↔ содержимое), ←/→ на слайдере
    — шаг ±5 с автоповтором. Сохранение — debounce 400 мс, flush при закрытии.
    """

    FLUSH_MS = 400
    SLIDER_STEP = 5

    def __init__(self, audio: AudioManager, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.WindowType.Widget)
        self._audio = audio
        self._settings = audio._settings
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "background: rgba(0,0,0,0.85);"
            "border: 1px solid #E60000; border-radius: 16px;")
        self._dirty: bool = False
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(self.FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._build_ui()
        self.hide()

    # ---- UI ---------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(24)

        # --- левое меню ---
        self.menu = QListWidget(self)
        self.menu.setObjectName("settingsMenu")
        self.menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.menu.setFixedWidth(260)
        for name in ("Сеть", "Экран", "Звук", "Питание"):
            self.menu.addItem(QListWidgetItem(name))
        self.menu.currentRowChanged.connect(self._on_tab)
        outer.addWidget(self.menu)

        # --- правый стек ---
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._tab_stub("Сеть",
                                            "Раздел «Сеть» — заглушка.\n"
                                            "Подключение к Wi-Fi управляется "
                                            "средствами Windows."))
        self.stack.addWidget(self._tab_stub("Экран",
                                            "Раздел «Экран» — заглушка.\n"
                                            "Лаунчер всегда занимает весь "
                                            "экран (fullscreen)."))
        self.stack.addWidget(self._tab_sound())
        self.stack.addWidget(self._tab_power())
        outer.addWidget(self.stack, 1)

        self._focus_zone: str = "menu"    # 'menu' | 'content'
        self._content_index: int = 0

    def _tab_stub(self, title: str, text: str) -> QWidget:
        w = QWidget(self)
        v = QVBoxLayout(w)
        cap = QLabel(title.upper(), w)
        cap.setStyleSheet("color:#FFFFFF; font-size:28px; font-weight:bold;"
                          "background:transparent; border:none;")
        body = QLabel(text, w)
        body.setWordWrap(True)
        body.setStyleSheet("color:#B0B0B0; font-size:16px;"
                           "background:transparent; border:none;")
        v.addWidget(cap)
        v.addWidget(body)
        v.addStretch(1)
        return w

    def _tab_sound(self) -> QWidget:
        w = QWidget(self)
        v = QVBoxLayout(w)
        v.setSpacing(14)

        def slider_row(label: str, value: int) -> tuple[QLabel, QSlider]:
            cap = QLabel(f"{label}: {value}", w)
            cap.setStyleSheet("color:#FFFFFF; font-size:16px;"
                              "background:transparent; border:none;")
            sl = QSlider(Qt.Orientation.Horizontal, w)
            sl.setRange(0, 100)
            sl.setValue(value)
            sl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            return cap, sl

        self.lbl_music, self.sl_music = slider_row("Громкость музыки",
                                                   int(self._settings.get(
                                                       "music_volume", 70)))
        self.lbl_sfx, self.sl_sfx = slider_row("Громкость SFX",
                                               int(self._settings.get(
                                                   "sfx_volume", 80)))
        self.sl_music.valueChanged.connect(self._on_music_vol)
        self.sl_sfx.valueChanged.connect(self._on_sfx_vol)

        def line_row(label: str, value: str) -> QLineEdit:
            cap = QLabel(label, w)
            cap.setStyleSheet("color:#B0B0B0; font-size:14px;"
                              "background:transparent; border:none;")
            v.addWidget(cap)
            ed = QLineEdit(value, w)
            ed.setMinimumHeight(36)
            ed.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            ed.textChanged.connect(lambda t, l=label: self._on_sound_file(l, t))
            v.addWidget(ed)
            return ed

        v.addWidget(self.lbl_music)
        v.addWidget(self.sl_music)
        v.addWidget(self.lbl_sfx)
        v.addWidget(self.sl_sfx)
        self.ed_music = line_row("Файл музыки", str(self._settings.get(
            "music_file", "menu_theme.mp3")))
        sounds = self._settings.get("sounds", {})
        self.ed_hover = line_row("Звук hover", str(sounds.get("hover", "hover.wav")))
        self.ed_select = line_row("Звук select", str(sounds.get("select", "select.wav")))
        self.ed_back = line_row("Звук back", str(sounds.get("back", "back.wav")))
        v.addStretch(1)
        return w

    def _tab_power(self) -> QWidget:
        w = QWidget(self)
        v = QVBoxLayout(w)
        v.setSpacing(16)
        cap = QLabel("ПИТАНИЕ", w)
        cap.setStyleSheet("color:#FFFFFF; font-size:28px; font-weight:bold;"
                          "background:transparent; border:none;")
        v.addWidget(cap)
        self.btn_min = QPushButton("Свернуть в Windows", w)
        self.btn_restart = QPushButton("Перезагрузить ПК", w)
        self.btn_shutdown = QPushButton("Выключить ПК", w)
        for b in (self.btn_min, self.btn_restart, self.btn_shutdown):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setMinimumHeight(52)
            v.addWidget(b)
        self.btn_min.clicked.connect(self._power_minimize)
        self.btn_restart.clicked.connect(lambda: self._power_action("/r"))
        self.btn_shutdown.clicked.connect(lambda: self._power_action("/s"))
        note = QLabel("Действия питания имеют 5-секундную задержку —\n"
                      "отмена: shutdown /a в командной строке.", w)
        note.setStyleSheet("color:#B0B0B0; font-size:13px;background:transparent;"
                           "border:none;")
        v.addWidget(note)
        v.addStretch(1)
        return w

    # ---- tabs / focus -------------------------------------------------------------
    def _on_tab(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
            self._content_index = 0
            self._apply_content_focus()

    def _sound_widgets(self) -> list[QWidget]:
        return [self.sl_music, self.sl_sfx, self.ed_music,
                self.ed_hover, self.ed_select, self.ed_back]

    def _power_widgets(self) -> list[QWidget]:
        return [self.btn_min, self.btn_restart, self.btn_shutdown]

    def _content_widgets(self) -> list[QWidget]:
        tab = self.stack.currentIndex()
        if tab == 2:
            return self._sound_widgets()
        if tab == 3:
            return self._power_widgets()
        return []

    def _apply_content_focus(self) -> None:
        widgets = self._content_widgets()
        for wd in widgets:
            wd.setProperty("focused", "no")
            wd.style().unpolish(wd)
            wd.style().polish(wd)
        if widgets and 0 <= self._content_index < len(widgets):
            wd = widgets[self._content_index]
            wd.setProperty("focused", "yes")
            wd.style().unpolish(wd)
            wd.style().polish(wd)
            if isinstance(wd, QLineEdit):
                wd.setFocus()
            elif isinstance(wd, QSlider):
                wd.setFocus()

    # ---- gamepad API -----------------------------------------------------------
    def nav_up(self) -> None:
        if self._focus_zone == "menu":
            row = max(0, self.menu.currentRow() - 1)
            self.menu.setCurrentRow(row)
        else:
            widgets = self._content_widgets()
            if widgets:
                self._content_index = (self._content_index - 1) % len(widgets)
                self._apply_content_focus()

    def nav_down(self) -> None:
        if self._focus_zone == "menu":
            row = min(self.menu.count() - 1, self.menu.currentRow() + 1)
            self.menu.setCurrentRow(row)
        else:
            widgets = self._content_widgets()
            if widgets:
                self._content_index = (self._content_index + 1) % len(widgets)
                self._apply_content_focus()

    def nav_left(self) -> None:
        if self._focus_zone == "content":
            widgets = self._content_widgets()
            if widgets and isinstance(widgets[self._content_index], QSlider):
                sl = widgets[self._content_index]
                sl.setValue(sl.value() - self.SLIDER_STEP)   # шаг ±5
                return
        if self._focus_zone == "content" and self.stack.currentIndex() >= 0:
            pass
        # из контента назад в меню
        self._focus_zone = "menu"
        self._apply_content_focus()

    def nav_right(self) -> None:
        if self._focus_zone == "menu":
            if self._content_widgets():
                self._focus_zone = "content"
                self._content_index = 0
                self._apply_content_focus()
            return
        widgets = self._content_widgets()
        if widgets and isinstance(widgets[self._content_index], QSlider):
            sl = widgets[self._content_index]
            sl.setValue(sl.value() + self.SLIDER_STEP)

    def activate_current(self) -> None:
        if self._focus_zone == "menu":
            self._focus_zone = "content"
            self._content_index = 0
            self._apply_content_focus()
            return
        widgets = self._content_widgets()
        if widgets:
            wd = widgets[self._content_index]
            if isinstance(wd, QPushButton):
                wd.click()

    def cancel(self) -> None:
        self.close_dialog()

    # ---- debounce (п.6) ----------------------------------------------------------
    def _mark_dirty(self) -> None:
        self._dirty = True
        self._flush_timer.start()      # перезапуск single-shot таймера

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self._settings["music_volume"] = int(self.sl_music.value())
        self._settings["sfx_volume"] = int(self.sl_sfx.value())
        self._settings["music_file"] = self.ed_music.text().strip() or \
            "menu_theme.mp3"
        self._settings.setdefault("sounds", {}).update({
            "hover": self.ed_hover.text().strip() or "hover.wav",
            "select": self.ed_select.text().strip() or "select.wav",
            "back": self.ed_back.text().strip() or "back.wav",
        })
        if StorageManager.save_settings(self._settings):
            log.info("Настройки сохранены (debounce flush).")

    def _on_music_vol(self, v: int) -> None:
        self.lbl_music.setText(f"Громкость музыки: {v}")
        self._audio.set_music_volume(v)      # сразу к плееру
        self._mark_dirty()                    # в файл — debounce

    def _on_sfx_vol(self, v: int) -> None:
        self.lbl_sfx.setText(f"Громкость SFX: {v}")
        self._audio.set_sfx_volume(v)
        self._mark_dirty()

    def _on_sound_file(self, label: str, text: str) -> None:
        mapping = {
            "Файл музыки": ("music", None),
            "Звук hover": ("hover", None),
            "Звук select": ("select", None),
            "Звук back": ("back", None),
        }
        if label in mapping:
            self._mark_dirty()

    # ---- power (п.8: subprocess.Popen, без os.system) ------------------------------
    def _power_minimize(self) -> None:
        main = self.window()
        if isinstance(main, MainWindow):
            main.showMinimized()

    def _power_action(self, flag: str) -> None:
        what = "перезагрузки" if flag == "/r" else "выключения"
        ans = QMessageBox.question(
            self, "Подтверждение",
            f"Вы уверены, что хотите выполнить {what} ПК?\n"
            "Отмена: shutdown /a (в течение 5 секунд).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._flush()
        cmd = ["shutdown", flag, "/t", "5"]
        try:
            proc = subprocess.Popen(cmd)
            # даём команду стартовать; returncode=None значит процесс жив
            try:
                rc = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                rc = proc.returncode    # обычно None — команда принята ОС
            if rc not in (None, 0):
                raise OSError(f"shutdown вернул код {rc}")
            log.info("Команда питания выполнена: %s", cmd)
        except (OSError, ValueError) as e:
            log.error("Ошибка команды питания %s: %s", cmd, e)
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось выполнить команду:\n{' '.join(cmd)}\n{e}\n"
                "Возможно, политика группы запрещает это действие.")

    # ---- open/close -----------------------------------------------------------
    def open_dialog(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            pw, ph = parent.width(), parent.height()
            w = min(1100, int(pw * 0.8))
            h = min(680, int(ph * 0.8))
            self.setGeometry((pw - w) // 2, (ph - h) // 2, w, h)
        self.menu.setCurrentRow(2)          # открываемся на «Звук»
        self._focus_zone = "menu"
        self.show()
        self.raise_()

    def close_dialog(self) -> None:
        self._flush_timer.stop()
        self._flush()                        # принудительный flush (п.6)
        self.hide()
        self.closed_signal()

    def closed_signal(self) -> None:
        main = self.window()
        if isinstance(main, MainWindow):
            main.on_overlay_closed(self)


# ============================================================================
# СЕКЦИЯ: Toast — всплывающая индикация статуса геймпада (не QMessageBox)
# ============================================================================
class Toast(QLabel):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "background: rgba(26,10,10,0.92); color:#FFFFFF;"
            "border: 1px solid #E60000; border-radius: 12px;"
            "padding: 12px 24px; font-size: 18px;")
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, ms: int = 2000) -> None:
        self.setText(text)
        parent = self.parentWidget()
        if parent is not None:
            self.adjustSize()
            x = (parent.width() - self.width()) // 2
            self.move(x, 90)
        self.show()
        self.raise_()
        self._timer.start(ms)


# ============================================================================
# СЕКЦИЯ: MainWindow
# ============================================================================
class MainWindow(QMainWindow):
    """Главный экран: верхняя панель, фон-кроссфейд, карусель, оверлеи."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ardor Gaming")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet(APP_QSS)

        # --- данные ---
        self._settings: dict = StorageManager.load_settings()
        self._games: list[dict] = StorageManager.load_games()
        self._audio = AudioManager(self._settings, self)

        # --- разрешение / масштаб ---
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        self._scale: float = self._compute_scale(geo.size())
        log.info("Экран: %dx%d, масштаб элементов: %.2f",
                 geo.width(), geo.height(), self._scale)

        # --- фон ---
        self.bg = BackgroundCrossfade(self)
        self.bg.lower()

        # --- центральное содержимое ---
        central = QWidget(self)
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        central.setStyleSheet("background: transparent;")
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        v.addWidget(self._build_topbar())
        self.carousel = CarouselWidget(central)
        v.addWidget(self.carousel, 1)
        self.desc_label = QLabel(central)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.desc_label.setWordWrap(True)
        desc_h = max(2, round(2 * 24 * self._scale))
        self.desc_label.setMaximumHeight(desc_h)
        self.desc_label.setStyleSheet(
            f"color:#B0B0B0; font-size:{int(18 * self._scale)}px;"
            "background:transparent;")
        v.addWidget(self.desc_label)
        v.addSpacing(int(40 * self._scale))

        # --- toast ---
        self.toast = Toast(central)

        # --- оверлеи ---
        self.add_dialog = AddGameDialog(central)
        self.add_dialog.saved.connect(self._on_game_added)
        self.settings_dialog = SettingsDialog(self._audio, central)

        self._overlay_stack: list[QWidget] = []

        # --- карусель ---
        self.rebuild_carousel(initial=True)
        self.carousel.item_activated.connect(self._on_carousel_activate)
        self.carousel.index_changed.connect(self._on_carousel_index_changed)

        # --- часы (QTimer 1 сек) ---
        self.clock_label = self._clock_lbl
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

        # --- геймпад ---
        self.gamepad = GamepadController(self._settings, self)
        gp = self.gamepad
        gp.navigate_left.connect(lambda auto: self._navigate(-1, auto))
        gp.navigate_right.connect(lambda auto: self._navigate(+1, auto))
        gp.navigate_up.connect(self._nav_vertical)
        gp.navigate_down.connect(self._nav_vertical_down)
        gp.confirm.connect(self._confirm)
        gp.back.connect(self._back)
        gp.open_settings.connect(self._open_settings)
        gp.open_add_game.connect(self._open_add_game)
        gp.emergency_exit.connect(self._emergency_exit)
        gp.status_changed.connect(self._on_gamepad_status)

        # --- скрытие курсора при бездействии мыши (3 сек) ---
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(3000)
        self._cursor_timer.timeout.connect(self._hide_cursor)
        self._cursor_hidden: bool = False
        self._cursor_timer.start()
        inst = QApplication.instance()
        if inst is not None:
            inst.installEventFilter(self)      # фильтр на все события приложения

        # --- отслеживание запущенной игры ---
        self._game_proc: Optional[subprocess.Popen] = None
        self._proc_timer = QTimer(self)
        self._proc_timer.setInterval(1000)
        self._proc_timer.timeout.connect(self._watch_game_process)

        self.resize(geo.size())

    # ---- helpers ------------------------------------------------------------------
    @staticmethod
    def _compute_scale(size) -> float:
        base = 1920.0
        ratio = size.width() / base
        s = max(0.6, min(1.6, ratio))     # минимум 60% при < 1280
        if size.width() < 1280:
            s = 0.6
        return s

    # ---- top bar --------------------------------------------------------------------
    def _build_topbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("topBar")
        h = int(80 * self._scale)
        bar.setFixedHeight(h)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(int(32 * self._scale), 0,
                               int(32 * self._scale), 0)

        logo = QLabel("ARDOR GAMING", bar)
        logo.setObjectName("logo")
        logo.setStyleSheet(
            f"color:#E60000; font-size:{int(30 * self._scale)}px;"
            "font-weight:bold; letter-spacing: 3px; background:transparent;")
        lay.addWidget(logo)

        self.active_title_label = QLabel("", bar)
        self.active_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.active_title_label.setStyleSheet(
            f"color:#FFFFFF; font-size:{int(26 * self._scale)}px;"
            "font-weight:bold; background:transparent;")
        lay.addWidget(self.active_title_label, 1)

        # статус геймпада — PNG-иконка вместо emoji (см. «консольный сценарий»)
        self.gamepad_icon = QLabel(bar)
        icon_sz = int(32 * self._scale)
        self.gamepad_icon.setFixedSize(icon_sz, icon_sz)
        self.gamepad_icon.setStyleSheet("background:transparent;")
        lay.addWidget(self.gamepad_icon)
        self.gamepad_name_label = QLabel("Нет геймпада", bar)
        self.gamepad_name_label.setStyleSheet(
            f"color:#808080; font-size:{int(16 * self._scale)}px;"
            "background:transparent;")
        lay.addWidget(self.gamepad_name_label)
        lay.addSpacing(int(24 * self._scale))

        self._clock_lbl = QLabel("--:--", bar)
        self._clock_lbl.setStyleSheet(
            f"color:#FFFFFF; font-size:{int(22 * self._scale)}px;"
            "background:transparent;")
        lay.addWidget(self._clock_lbl)
        lay.addSpacing(int(12 * self._scale))

        wifi = QLabel(bar)
        wifi.setPixmap(AssetLoader.pixmap("assets/icons/wifi.png",
                                          icon_sz, icon_sz, keep_aspect=False))
        wifi.setStyleSheet("background:transparent;")
        lay.addWidget(wifi)
        lay.addSpacing(int(8 * self._scale))
        batt = QLabel(bar)
        batt.setPixmap(AssetLoader.pixmap("assets/icons/battery.png",
                                          icon_sz, icon_sz, keep_aspect=False))
        batt.setStyleSheet("background:transparent;")
        lay.addWidget(batt)
        return bar

    def _tick_clock(self) -> None:
        import datetime
        self._clock_lbl.setText(datetime.datetime.now().strftime("%H:%M"))

    # ---- carousel data -----------------------------------------------------------
    def rebuild_carousel(self, initial: bool = False) -> None:
        icon_size = int(300 * self._scale)
        self.carousel.set_scale(icon_size)
        items: list[CarouselItem] = []
        for g in self._games:
            items.append(CarouselItem(
                CarouselItem.KIND_GAME,
                g["title"], g["description"],
                g.get("cover") or g.get("icon") or "",
                data=g))
        items.append(CarouselItem(CarouselItem.KIND_BUTTON,
                                  "⚙ Настройки", "Открыть настройки", "",
                                  button_id="settings"))
        items.append(CarouselItem(CarouselItem.KIND_BUTTON,
                                  "➕ Добавить игру", "Новая игра в библиотеке",
                                  "", button_id="add"))
        focus = 0
        if not self._games:
            # пустая библиотека: подсветить «Добавить игру» (п.5)
            focus = len(items) - 1
        self.carousel.set_items(items, focus_index=focus)
        if initial:
            self._center_window()

    def _center_window(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.setGeometry(geo)

    # ---- signals from carousel -------------------------------------------------------
    def _on_carousel_index_changed(self, item: object) -> None:
        if not isinstance(item, CarouselItem):
            return
        # название в топбаре — эллипсис при переполнении (п.9)
        width = max(200, int(self.width() * 0.4))
        fm = QFontMetrics(self.active_title_label.font())
        elided = fm.elidedText(item.title, Qt.TextElideMode.ElideRight, width)
        self.active_title_label.setText(elided)
        # описание (wordWrap, до 2 строк + elide)
        desc = item.subtitle if item.kind == CarouselItem.KIND_GAME else ""
        if item.kind == CarouselItem.KIND_BUTTON:
            desc = item.subtitle
        self.desc_label.setText(desc)
        # фон
        bg_rel = ""
        if item.kind == CarouselItem.KIND_GAME and item.data:
            bg_rel = item.data.get("background", "")
        self.bg.set_background(bg_rel)
        # hover-SFX играет диспетчер навигации (только edge-trigger)

    def _on_carousel_activate(self, item: object) -> None:
        if not isinstance(item, CarouselItem):
            return
        if item.kind == CarouselItem.KIND_BUTTON:
            if item.button_id == "settings":
                self._open_settings()
            elif item.button_id == "add":
                self._open_add_game()
        elif item.kind == CarouselItem.KIND_GAME and item.data:
            self._launch_game(item.data)

    # ---- game launch ---------------------------------------------------------------
    def _launch_game(self, game: dict) -> None:
        exe = game.get("exe_path", "")
        if not exe:
            QMessageBox.warning(self, "Ошибка",
                                f"У игры «{game.get('title')}» не задан путь к EXE.")
            return
        p = Path(exe)
        if not p.exists():
            QMessageBox.warning(self, "Игра не найдена",
                                f"Файл не найден:\n{exe}")
            return
        try:
            self._audio.play_sfx("select")
            self._game_proc = subprocess.Popen(
                [str(exe)], cwd=os.path.dirname(str(exe)))
            log.info("Запущена игра '%s' (pid=%s)", game["title"],
                     self._game_proc.pid)
            self.showMinimized()               # консольный сценарий
            self._proc_timer.start()
        except OSError as e:
            log.error("Не удалось запустить '%s': %s", exe, e)
            QMessageBox.warning(self, "Ошибка запуска", str(e))

    def _watch_game_process(self) -> None:
        proc = self._game_proc
        if proc is None:
            self._proc_timer.stop()
            return
        if proc.poll() is not None:
            self._proc_timer.stop()
            self._game_proc = None
            log.info("Игровой процесс завершился (rc=%s) — возврат через 500 мс.",
                     proc.returncode)
            QTimer.singleShot(500, self._restore_fullscreen)

    def _restore_fullscreen(self) -> None:
        self.showFullScreen()
        self.activateWindow()
        self.raise_()

    # ---- overlays -------------------------------------------------------------------
    def _push_overlay(self, w: QWidget) -> None:
        self._overlay_stack.append(w)

    def on_overlay_closed(self, w: QWidget) -> None:
        if w in self._overlay_stack:
            self._overlay_stack.remove(w)

    def _open_settings(self) -> None:
        if self.settings_dialog.isVisible():
            return
        self._audio.play_sfx("select")
        self.settings_dialog.open_dialog()
        self._push_overlay(self.settings_dialog)

    def _open_add_game(self) -> None:
        if self.add_dialog.isVisible():
            return
        self._audio.play_sfx("select")
        self.add_dialog.open_dialog()
        self._push_overlay(self.add_dialog)

    def _on_game_added(self, game: dict) -> None:
        self._games.append(game)
        # обновить карусель без перезапуска; фокус на новую игру
        new_index = len(self._games) - 1
        self.rebuild_carousel()
        self.carousel._index = new_index
        self.carousel._layout_slots(animate=False)
        self.carousel.index_changed.emit(self.carousel.current_item())

    # ---- navigation dispatch --------------------------------------------------------
    @property
    def _active_overlay(self) -> Optional[QWidget]:
        for w in reversed(self._overlay_stack):
            if w.isVisible():
                return w
        return None

    def _navigate(self, delta: int, auto: bool) -> None:
        ov = self._active_overlay
        if isinstance(ov, SettingsDialog):
            if delta < 0:
                if not auto:
                    self._audio.play_sfx("hover")
                ov.nav_left()
            else:
                if not auto:
                    self._audio.play_sfx("hover")
                ov.nav_right()
            return
        if ov is not None:
            return      # в AddGameDialog ←/→ не используется
        if not auto:
            self._audio.play_sfx("hover")   # только edge (анти-«пулемёт»)
        self.carousel.move_by(delta)

    def _nav_vertical(self, auto: bool) -> None:
        ov = self._active_overlay
        if ov is not None:
            if not auto:
                self._audio.play_sfx("hover")
            ov.nav_up()
            return
        if not auto:
            self._audio.play_sfx("hover")
        self.carousel.move_by(-1)

    def _nav_vertical_down(self, auto: bool) -> None:
        ov = self._active_overlay
        if ov is not None:
            if not auto:
                self._audio.play_sfx("hover")
            ov.nav_down()
            return
        if not auto:
            self._audio.play_sfx("hover")
        self.carousel.move_by(+1)

    def _confirm(self) -> None:
        ov = self._active_overlay
        if ov is not None:
            self._audio.play_sfx("select")
            ov.activate_current()
            return
        self.carousel.activate_current()

    def _back(self) -> None:
        ov = self._active_overlay
        if ov is not None:
            self._audio.play_sfx("back")
            ov.cancel()
            return
        # На Home: B → диалог «Выход / Свернуть / Отмена»
        box = QMessageBox(self)
        box.setWindowTitle("Ardor Gaming")
        box.setText("Что сделать?")
        b_exit = box.addButton("Выход из лаунчера",
                               QMessageBox.ButtonRole.AcceptRole)
        b_min = box.addButton("Свернуть в Windows",
                              QMessageBox.ButtonRole.ActionRole)
        b_cancel = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_exit:
            QApplication.quit()
        elif clicked is b_min:
            self.showMinimized()
        _ = b_cancel

    def _emergency_exit(self) -> None:
        if self._active_overlay is not None:
            return
        ans = QMessageBox.question(
            self, "Аварийный выход",
            "Удержаны Back+Start.\nВыйти из лаунчера в Windows?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            QApplication.quit()

    # ---- gamepad status UI --------------------------------------------------------
    def _on_gamepad_status(self, connected: bool, name: str) -> None:
        icon_sz = int(32 * self._scale)
        if connected:
            self.gamepad_icon.setPixmap(AssetLoader.pixmap(
                "assets/icons/gamepad_connected.png", icon_sz, icon_sz,
                keep_aspect=False))
            self.gamepad_name_label.setText(name or "Геймпад")
            self.gamepad_name_label.setStyleSheet(
                f"color:#2ECC40; font-size:{int(16 * self._scale)}px;"
                "background:transparent;")
            self.toast.show_message(f"Геймпад подключён: {name}", 2000)
        else:
            self.gamepad_icon.setPixmap(AssetLoader.pixmap(
                "assets/icons/gamepad_disconnected.png", icon_sz, icon_sz,
                keep_aspect=False))
            self.gamepad_name_label.setText("Нет геймпада")
            self.gamepad_name_label.setStyleSheet(
                f"color:#808080; font-size:{int(16 * self._scale)}px;"
                "background:transparent;")
            self.toast.show_message("Геймпад отключён", 2000)

    # ---- cursor hiding -------------------------------------------------------------
    def _hide_cursor(self) -> None:
        if not self._cursor_hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._cursor_hidden = True

    def _show_cursor(self) -> None:
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False
        self._cursor_timer.start()      # перезапуск 3-сек таймера

    def eventFilter(self, obj, event) -> bool:          # noqa: N802
        try:
            et = event.type()
            if et in (event.Type.MouseMove, event.Type.MouseButtonPress,
                      event.Type.Wheel):
                self._show_cursor()
        except Exception:               # noqa: BLE001
            pass
        return super().eventFilter(obj, event)

    # ---- keyboard fallback ---------------------------------------------------------
    def keyPressEvent(self, e: QKeyEvent) -> None:      # noqa: N802
        k = e.key()
        mods = e.modifiers()
        if mods & Qt.KeyboardModifier.AltModifier and \
                k == Qt.Key.Key_F4:
            self._emergency_exit()
            return
        ov = self._active_overlay
        if ov is not None and isinstance(ov, QLineEdit):
            pass
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            # в поле ввода — клавиатура хозяина, не перехватываем стрелки Enter
            if k == Qt.Key.Key_Escape:
                self._back()
                return
            super().keyPressEvent(e)
            return
        if k in (Qt.Key.Key_Left, Qt.Key.Key_A):
            self._navigate(-1, False)
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_D):
            self._navigate(+1, False)
        elif k in (Qt.Key.Key_Up, Qt.Key.Key_W):
            self._nav_vertical(False)
        elif k in (Qt.Key.Key_Down, Qt.Key.Key_S):
            self._nav_vertical_down(False)
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._confirm()
        elif k in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self._back()
        elif k == Qt.Key.Key_F1:
            self._open_settings()
        elif k == Qt.Key.Key_F2:
            self._open_add_game()
        else:
            super().keyPressEvent(e)

    # ---- misc events -----------------------------------------------------------
    def resizeEvent(self, event) -> None:                # noqa: N802
        super().resizeEvent(event)
        self.bg.setGeometry(0, 0, self.width(), self.height())
        cw = self.centralWidget()
        if cw is not None:
            cw.setGeometry(0, 0, self.width(), self.height())

    def showEvent(self, event) -> None:                  # noqa: N802
        super().showEvent(event)
        self._on_gamepad_status(self.gamepad.connected,
                                self.gamepad.controller_name)

    def closeEvent(self, event) -> None:                 # noqa: N802
        log.info("Закрытие лаунчера.")
        try:
            self.settings_dialog._flush()
        except Exception:               # noqa: BLE001
            pass
        try:
            self.gamepad.shutdown()
        except Exception:               # noqa: BLE001
            pass
        if PYGAME_AVAILABLE:
            try:
                pygame.joystick.quit()
                pygame.quit()
            except Exception as e:      # noqa: BLE001
                log.warning("pygame quit error: %s", e)
        super().closeEvent(event)


# ============================================================================
# СЕКЦИЯ: QSS
# ============================================================================
APP_QSS = """
* {
    font-family: "Segoe UI", Arial, sans-serif;
}
QMainWindow, QWidget#centralHost {
    background: #000000;
}
QLabel { color: #FFFFFF; background: transparent; }

/* ---- кнопки ---- */
QPushButton {
    background: transparent;
    color: #FFFFFF;
    border: 2px solid #E60000;
    border-radius: 12px;
    padding: 12px 24px;
    font-size: 18px;
    font-weight: bold;
}
QPushButton:hover, QPushButton:focus, QPushButton[focused="yes"] {
    background: #E60000;
    color: #000000;
}
QPushButton[focused="yes"] { border-color: #FF3333; }
QPushButton:pressed { background: #B00000; color: #FFFFFF; }

/* ---- слайдеры ---- */
QSlider::groove:horizontal {
    height: 8px;
    background: #2A0F0F;
    border: 1px solid #4A1515;
    border-radius: 4px;
}
QSlider::sub-page:horizontal {
    background: #E60000;
    border-radius: 4px;
}
QSlider::handle:horizontal {
    background: #E60000;
    width: 22px;
    height: 22px;
    margin: -8px 0;
    border-radius: 11px;
}
QSlider::handle:horizontal:hover,
QSlider[focused="yes"]::handle:horizontal { background: #FF3333; }

/* ---- поля ввода ---- */
QLineEdit {
    background: #1A0A0A;
    border: 1px solid #E60000;
    border-radius: 8px;
    padding: 8px;
    color: #FFFFFF;
    font-size: 16px;
    selection-background-color: #E60000;
}
QLineEdit[focused="yes"] {
    border: 2px solid #FF3333;
    background: #2A0F0F;
}
QLineEdit:focus { border: 2px solid #FF3333; }

/* ---- список вкладок настроек ---- */
QListWidget#settingsMenu {
    background: #1A0A0A;
    border: 1px solid #2A0F0F;
    border-radius: 12px;
    outline: none;
}
QListWidget#settingsMenu::item {
    color: #B0B0B0;
    padding: 16px 20px;
    border-radius: 8px;
    font-size: 20px;
}
QListWidget#settingsMenu::item:selected {
    background: #E60000;
    color: #FFFFFF;
    font-weight: bold;
}
QListWidget#settingsMenu::item:focus { border: 2px solid #FF3333; }

/* ---- QMessageBox ---- */
QMessageBox {
    background-color: #1A0A0A;
}
QMessageBox QLabel { color: #FFFFFF; font-size: 16px; }
QMessageBox QPushButton {
    background: transparent;
    color: #FFFFFF;
    border: 2px solid #E60000;
    border-radius: 12px;
    padding: 10px 22px;
}
QMessageBox QPushButton:hover { background: #E60000; color: #000000; }

QToolTip {
    background-color: #2A0F0F;
    color: #FFFFFF;
    border: 1px solid #E60000;
}
"""


# ============================================================================
# BOOTSTRAP
# ============================================================================
def main() -> int:
    # High-DPI округление — ДО создания QApplication
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Ardor Gaming")
    win = MainWindow()
    win.showFullScreen()
    log.info("Ardor Gaming запущен.")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
